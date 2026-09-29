import csv
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from lab_control.domain.instruments import DeviceIdentity, Measurement, MeasurementBatch
from lab_control.storage import analysis
from lab_control.storage.layout import raw_folder
from lab_control.storage.paths import SaveContext
from lab_control.storage.recording import SegmentWriter, convert_csv
from lab_control.storage.repository import Repository

START = datetime.fromisoformat("2026-09-27T23:59:59.123456+08:00")
IDENTITY = DeviceIdentity("Fixture", "数据 测试", "SERIAL")


def frame(*samples, elapsed=1.7):
    return MeasurementBatch(
        IDENTITY,
        tuple(samples),
        {"range": 10},
        timestamp="2026-09-27T16:00:00.123456+00:00",
        timing={"version": 1, "recording_elapsed_seconds": elapsed, "read_duration_seconds": 0.123},
    )


def curve(channel=1, points=3, quantity="waveform", unit="V", axis_unit="s"):
    return Measurement(
        quantity,
        unit,
        np.arange(points, dtype=float) + channel,
        channel,
        np.arange(points, dtype=float) * channel / 10,
        axis_unit,
        metadata={
            "received_at": f"2026-09-27T16:00:00.{channel}00000+00:00",
            "trigger_time_local": "2026-09-28T00:00:00.01" if channel == 1 else None,
            "trigger_time_source": "instrument_local_timezone_unknown",
        },
    )


def recording(tmp_path, *frames, size=64 * 1024 * 1024):
    context = SaveContext(tmp_path, "electronic", "continuous", identity=IDENTITY, started_at=START)
    context.prepare()
    writer = SegmentWriter(context.raw_folder, size=size)
    for number, batch in enumerate(frames, 1):
        writer.append(number, batch)
    writer.close()
    return context.folder


def table(path):
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def test_scalar_wide_columns_local_time_monotonic_time_and_no_index(tmp_path):
    first = frame(
        Measurement("voltage", "V", [1.123456789012345]),
        Measurement("current", "A", [0.2]),
        Measurement("voltage", "V", [5.6], 2),
    )
    second = replace(first, timestamp="2026-09-27T15:59:58+00:00", timing={"recording_elapsed_seconds": 3.2})
    folder = recording(tmp_path, first, second, size=1)
    assert convert_csv(folder)["converted"] == 2
    assert len(list((folder / "_raw").glob("*.sqlite3"))) == 2
    assert len(list(folder.glob("数据_*.csv"))) == 1  # Independent of binary segmentation.
    rows = table(folder / "数据_00001.csv")
    assert len(rows) == 2 and len(rows[0]) == 6
    assert rows[0]["本地时间(UTC+08:00)"] == "2026-09-28 00:00:00.123456"
    assert rows[1]["本地时间(UTC+08:00)"] < rows[0]["本地时间(UTC+08:00)"]
    assert [float(row["距记录开始(s)"]) for row in rows] == [1.7, 3.2]
    assert float(rows[0]["CH1电压(V)"]) == first.measurements[0].values[0]
    assert rows[0]["CH2电压(V)"] == "5.6"
    assert not (folder / analysis.INDEX).exists()
    assert not list((folder / "_raw").glob("*.csv"))


def test_separate_axes_unequal_lengths_channel_timing_and_frame_index(tmp_path):
    folder = recording(tmp_path, frame(curve(1), curve(2, 2)))
    convert_csv(folder)
    rows = table(folder / "数据_00001.csv")
    assert len(rows) == 3
    assert rows[1]["CH1波形_时间(s)"] == "0.1" and rows[1]["CH2波形_时间(s)"] == "0.2"
    assert rows[2]["CH2波形(V)"] == rows[2]["CH2波形_时间(s)"] == ""
    index = table(folder / analysis.INDEX)[0]
    assert (index["起始行"], index["结束行"], index["起始点"], index["结束点"]) == ("2", "4", "1", "3")
    assert "CH1=" in index["通道接收时间(UTC)"] and "CH2=" in index["通道接收时间(UTC)"]
    assert "CH2=" not in index["仪器触发时间(原样)"]
    assert "instrument_local_timezone_unknown" in index["时间来源"]


@pytest.mark.parametrize(
    "quantity,unit,axis_unit,axis_name",
    [
        ("spectrum", "dBm", "Hz", "频率"),
        ("spectrum", "dBm", "s", "时间"),
        ("phase_noise", "dBc/Hz", "Hz", "偏移频率"),
        ("am_noise", "dBc/Hz", "Hz", "偏移频率"),
        ("stability", "ratio", "s", "平均时间"),
        ("frequency", "Hz", "s", "时间"),
    ],
)
def test_curve_families_use_declared_axes(tmp_path, quantity, unit, axis_unit, axis_name):
    folder = recording(tmp_path, frame(curve(quantity=quantity, unit=unit, axis_unit=axis_unit)))
    convert_csv(folder)
    rows = table(folder / "数据_00001.csv")
    assert any(key.endswith(f"_{axis_name}({axis_unit})") for key in rows[0])


def test_complex_unknown_trace_dmm_and_parameter_text(tmp_path):
    folder = recording(
        tmp_path,
        frame(Measurement("selected_trace", "", np.array([0j, 3 + 4j]), axis=[1e6, 2e6], axis_unit="Hz")),
        frame(Measurement("reading", "V", [8.0, 9.0])),
        frame(Measurement("output", "", [], metadata={"display_value": "=formula", "source": "readback"})),
    )
    convert_csv(folder)
    rows = table(folder / "数据_00001.csv")
    assert not any("S11" in key or "S21" in key for key in rows[0])
    assert rows[0]["CH1selected_trace_相位(deg)"] == ""
    assert float(rows[1]["CH1selected_trace_线性幅值"]) == 5
    assert float(rows[1]["CH1selected_trace_相位(deg)"]) == pytest.approx(53.13010235415598)
    rows = table(folder / "数据_00002.csv")
    assert list(rows[0]) == ["帧号", "点序号", "CH1读数(V)"]
    rows = table(folder / "数据_00003.csv")
    assert rows[0]["CH1output"] == "'=formula" and rows[0]["CH1output_来源"] == "readback"


def test_rollover_prefers_frames_splits_large_frame_and_preserves_point_numbers(tmp_path):
    folder = recording(tmp_path, frame(curve(points=3)), frame(curve(points=3)), frame(curve(points=12)))
    analysis.convert_analysis(folder, max_rows=5)
    parts = sorted(folder.glob("数据_*.csv"))
    assert [len(table(path)) for path in parts] == [3, 3, 5, 5, 2]
    assert [int(row["点序号"]) for path in parts[2:] for row in table(path)] == list(range(1, 13))
    index = table(folder / analysis.INDEX)
    assert [(row["起始点"], row["结束点"]) for row in index[2:]] == [("1", "5"), ("6", "10"), ("11", "12")]
    original = {p.name: p.read_bytes() for p in parts}
    assert analysis.convert_analysis(folder, max_rows=5)["converted"] == 3
    assert original == {p.name: p.read_bytes() for p in parts}


def test_byte_limit_bom_header_and_schema_changes(tmp_path):
    folder = recording(
        tmp_path,
        frame(curve(points=20)),
        frame(curve(points=2)),
        frame(curve(points=2, unit="mV")),
        frame(curve(2, 2, unit="mV")),
    )
    analysis.convert_analysis(folder, max_bytes=200)
    paths = sorted(folder.glob("数据_*.csv"))
    assert all(p.stat().st_size <= 200 for p in paths)
    assert sum(len(table(path)) for path in paths) == 26
    assert list(table(paths[-1])[0]) != list(table(paths[-2])[0])


@pytest.mark.parametrize("failure", ["checkpoint", "index", "new_part"])
def test_recovery_rolls_back_all_frame_outputs_without_duplicates(tmp_path, monkeypatch, failure):
    folder = recording(tmp_path, frame(curve(points=2)))
    analysis.convert_analysis(folder, max_rows=3)
    writer = SegmentWriter(raw_folder(folder))
    # Use a separate segment without replacing the first committed database.
    writer.paths = [str(raw_folder(folder) / "segment-00001.sqlite3")]
    writer.append(2, frame(curve(points=8)))
    writer.close()
    original_atomic = analysis.atomic_json
    original_index = analysis.AnalysisWriter._index
    original_create = analysis.AnalysisWriter._create

    def fail_checkpoint(path, value):
        if path.name == analysis.CHECKPOINT:
            raise OSError("interrupted checkpoint")
        return original_atomic(path, value)

    def fail_index(self, *args):
        original_index(self, *args)
        raise OSError("interrupted index")

    def fail_part(self, name, header):
        original_create(self, name, header)
        raise OSError("interrupted new_part")

    with monkeypatch.context() as patch:
        if failure == "checkpoint":
            patch.setattr(analysis, "atomic_json", fail_checkpoint)
        else:
            patch.setattr(
                analysis.AnalysisWriter,
                "_index" if failure == "index" else "_create",
                fail_index if failure == "index" else fail_part,
            )
        with pytest.raises(OSError, match="interrupted"):
            analysis.convert_analysis(folder, max_rows=3)
    assert analysis.convert_analysis(folder, max_rows=3)["converted"] == 2
    rows = [row for path in sorted(folder.glob("数据_*.csv")) for row in table(path)]
    assert [row["帧号"] for row in rows] == ["1"] * 2 + ["2"] * 8
    assert len(table(folder / analysis.INDEX)) == 4
    assert not (raw_folder(folder) / analysis.PENDING).exists()


def test_existing_unowned_file_and_concurrent_writer_are_not_overwritten(tmp_path):
    folder = recording(tmp_path, frame(curve()))
    conflict = folder / "数据_00001.csv"
    conflict.write_text("manual", encoding="utf-8")
    with pytest.raises(FileExistsError):
        convert_csv(folder)
    assert conflict.read_text() == "manual"
    with analysis._export_lock(raw_folder(folder)), pytest.raises(OSError):
        convert_csv(folder)
    assert conflict.read_text() == "manual"


@pytest.mark.parametrize("legacy", [False, True])
def test_single_frame_and_offline_reexport_preserve_source_and_old_frame_time(tmp_path, legacy):
    repository = Repository(tmp_path / "runtime")
    try:
        context = SaveContext(
            repository.data_root,
            "electronic",
            "single_frame",
            identity=IDENTITY,
            started_at=START,
            layout_version=1 if legacy else 2,
        )
        folder = repository.export(frame(curve()), context=context)
        if not legacy:
            assert {p.name for p in folder.iterdir()} == {"_raw", "记录说明.txt", "数据_00001.csv"}
            assert START.isoformat() in (folder / "记录说明.txt").read_text(encoding="utf-8-sig")
            assert not list((folder / "_raw").glob("*.csv"))
        original = {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*") if p.is_file()}
        exported = analysis.export_analysis(folder, tmp_path / "人工 分析")
        assert len(table(exported / "数据_00001.csv")) == 3
        assert not (exported / analysis.INDEX).exists()
        assert "2026-09-27T16:00:00.123456+00:00" in (exported / "记录说明.txt").read_text(
            encoding="utf-8-sig"
        )
        assert original == {
            str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*") if p.is_file()
        }
        with pytest.raises((ValueError, FileExistsError)):
            analysis.export_analysis(folder, exported)
        with pytest.raises(ValueError):
            analysis.export_analysis(folder, folder / "nested")
    finally:
        repository.close()


def test_json_only_complex_history_without_times_and_cli_never_opens_runtime(tmp_path, monkeypatch):
    from lab_control.cli.main import main

    source = tmp_path / "old"
    source.mkdir()
    (source / "snapshot.json").write_text(
        json.dumps(
            {
                "identity": {},
                "measurements": [
                    {
                        "quantity": "trace",
                        "unit": "",
                        "channel": 1,
                        "values": [{"real": 1, "imag": 2}],
                        "axis": [3],
                        "axis_unit": "Hz",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "lab_control.cli.main.ApplicationRuntime", lambda **kw: pytest.fail("hardware runtime opened")
    )
    output = tmp_path / "output"
    assert main(["export-analysis", str(source), "--output", str(output)]) == 0
    assert float(table(output / "数据_00001.csv")[0]["CH1trace_实部"]) == 1


def test_corrupt_checkpoint_cannot_escape_export_directory(tmp_path):
    folder = recording(tmp_path, frame(curve()))
    (raw_folder(folder) / analysis.PENDING).write_text(json.dumps(["../../unrelated.csv"]))
    with pytest.raises(ValueError, match="filename"):
        convert_csv(folder)


def test_index_rollover_and_verification_detect_corrupt_values_and_locations(tmp_path, monkeypatch):
    folder = recording(tmp_path, *(frame(curve()) for _ in range(5)))
    monkeypatch.setattr(analysis, "MAX_ROWS", 2)
    convert_csv(folder)
    assert len(list(folder.glob("帧索引*.csv"))) == 3
    assert analysis.verify_analysis(folder)["frames"] == 5
    path = folder / "数据_00001.csv"
    original = path.read_bytes()
    path.write_bytes(original.replace(b"1,1,0.0,1.0", b"1,1,0.0,9.0", 1))
    with pytest.raises(Exception, match="sample mismatch"):
        analysis.verify_analysis(folder)
    path.write_bytes(original)
    index = folder / analysis.INDEX
    index.write_bytes(index.read_bytes().replace(b",2,4,1,3,", b",2,5,1,3,", 1))
    with pytest.raises(Exception, match="index mismatch"):
        analysis.verify_analysis(folder)


def test_empty_recording_can_be_exported_without_creating_sample_files(tmp_path):
    folder = recording(tmp_path)
    destination = analysis.export_analysis(folder, tmp_path / "empty-analysis")
    assert not list(destination.glob("*.csv"))
    assert "有效帧数：0" in (destination / "记录说明.txt").read_text(encoding="utf-8-sig")


def test_analysis_write_failure_keeps_raw_and_allows_offline_recovery(tmp_path, monkeypatch):
    folder = recording(tmp_path, frame(curve()))
    source = next(raw_folder(folder).glob("*.sqlite3"))
    original = source.read_bytes()
    open_path = Path.open

    def fail(path, *args, **kwargs):
        if path.name.startswith("数据_") and args and args[0] == "ab":
            raise PermissionError("CSV is open in another application")
        return open_path(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", fail)
        with pytest.raises(PermissionError, match="CSV is open"):
            convert_csv(folder)
    assert original == source.read_bytes()
    assert convert_csv(folder)["converted"] == 1
    assert len(table(folder / "数据_00001.csv")) == 3
    assert analysis.verify_analysis(folder)["equal"]


def test_old_continuous_offline_export_and_raw_axis_shape(tmp_path):
    source = tmp_path / "legacy-segments"
    writer = SegmentWriter(source, size=1)
    writer.append(1, frame(Measurement("reading", "V", np.array([[1, 2], [3, 4]], dtype=">f8"))))
    writer.append(2, frame(Measurement("reading", "V", np.array([5, 6], dtype="<f4"))))
    writer.close()
    original = {p.name: p.read_bytes() for p in source.iterdir()}
    destination = analysis.export_analysis(source, tmp_path / "old-analysis")
    rows = table(destination / "数据_00001.csv")
    assert [float(row["CH1读数(V)"]) for row in rows] == list(range(1, 7))
    assert len(table(destination / analysis.INDEX)) == 2
    assert original == {p.name: p.read_bytes() for p in source.iterdir()}


def test_offline_resume_refreshes_failed_summary_and_human_notice(tmp_path):
    folder = recording(tmp_path, frame(curve()))
    path = raw_folder(folder) / "summary.json"
    path.write_text(
        json.dumps(
            {"binary_done": True, "committed": 1, "received": 1, "converted": 0, "csv_error": "old error"}
        )
    )
    assert convert_csv(folder)["converted"] == 1
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["converted"] == 1 and saved["csv_error"] == "" and saved["analysis_state"] == "complete"
    note = (folder / "记录说明.txt").read_text(encoding="utf-8-sig")
    assert "分析 CSV 已完成" in note and "生成中" not in note


def test_configured_stream_axis_source_is_visible_without_opening_raw_metadata(tmp_path):
    origin = "configured cadence; not a hardware timestamp"
    measured = replace(curve(quantity="frequency", unit="Hz"), metadata={"axis_origin": origin})
    folder = recording(tmp_path, frame(measured))
    convert_csv(folder)
    assert origin in table(folder / analysis.INDEX)[0]["时间来源"]
    assert origin in (folder / "记录说明.txt").read_text(encoding="utf-8-sig")
