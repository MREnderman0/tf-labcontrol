import csv
import json
import threading
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from lab_control.application.acquisition_pipeline import immutable_batch
from lab_control.domain.instruments import AcquisitionRequest, DeviceIdentity, Measurement, MeasurementBatch
from lab_control.storage.layout import raw_folder
from lab_control.storage.recording import Recording, SegmentWriter, convert_csv, read_frames
from lab_control.storage.repository import Repository

from .test_monitoring_timing import connected


def timed_frame(completed=12.3, stamp="2026-01-01T01:02:03.123456+00:00"):
    return MeasurementBatch(
        DeviceIdentity("Fixture", "Scope"),
        tuple(
            Measurement(
                "waveform",
                "V",
                np.array([1.0, 2.0]),
                ch,
                np.array([-0.002, 0.003]),
                "s",
                metadata={
                    "received_at": f"2026-01-01T01:02:03.{ch}00000+00:00",
                    "received_time_source": "host_utc_at_waveform_reply",
                    "trigger_time_local": "2026-01-01T09:01:59.125" if ch == 1 else None,
                    "trigger_time_source": "instrument_local_timezone_unknown" if ch == 1 else "unavailable",
                },
            )
            for ch in (1, 2)
        ),
        {},
        timestamp=stamp,
        validation="full",
        performance={"received_at_monotonic": completed},
        timing={
            "version": 1,
            "host_read_started_at": "2026-01-01T01:02:02.8+00:00",
            "host_read_completed_at": stamp,
            "read_duration_seconds": 0.323456,
        },
    )


def test_session_clocks_measure_actual_work_even_when_wall_clock_moves_back(tmp_path, monkeypatch):
    runtime, key = connected(tmp_path)
    with runtime:
        session = runtime.sessions.sessions[key]
        session.adapter.acquire = lambda request: timed_frame()
        request = AcquisitionRequest("spectrum")
        for elapsed in (0.217, 0.831, 0.391):
            clocks = iter((100.0, 100.0 + elapsed))
            utc = iter(("2026-01-01T01:00:02+00:00", "2026-01-01T01:00:01+00:00"))
            with monkeypatch.context() as m:
                m.setattr(
                    "lab_control.application.sessions.time",
                    SimpleNamespace(perf_counter=lambda: next(clocks)),
                )
                m.setattr("lab_control.application.sessions.utc_now", lambda: next(utc))
                batch = runtime.sessions._acquire(session, request)
            assert batch.timestamp == "2026-01-01T01:00:01+00:00"
            assert batch.timing["read_duration_seconds"] == pytest.approx(elapsed)
            assert batch.timing["host_read_started_at"] > batch.timing["host_read_completed_at"]
            assert batch.measurements[0].metadata["trigger_time_local"] == "2026-01-01T09:01:59.125"


@pytest.mark.parametrize("layout_version", [1, 2])
def test_single_exports_preserve_old_frame_and_channel_timing(tmp_path, layout_version):
    repository = Repository(tmp_path)
    try:
        batch = immutable_batch(timed_frame())
        with pytest.raises(TypeError):
            batch.timing["version"] = 99
        context = replace(repository.new_save_context("electronic", "single_frame", identity=batch.identity),
                          layout_version=layout_version)
        folder = repository.export(batch, context=context)
        raw = raw_folder(folder)
        snapshot = json.loads((raw / "snapshot.json").read_text(encoding="utf-8"))
        with np.load(raw / "samples.npz", allow_pickle=False) as arrays:
            metadata = json.loads(str(arrays["metadata_json"]))
            assert metadata["timing"] == snapshot["timing"] == batch.timing
            np.testing.assert_array_equal(arrays["axis_0"], [-0.002, 0.003])
            assert metadata["measurements"][0]["metadata"]["trigger_time_local"] == "2026-01-01T09:01:59.125"
            assert metadata["measurements"][1]["metadata"]["trigger_time_local"] is None
        if layout_version == 2:
            assert batch.timestamp in (folder / "记录说明.txt").read_text(encoding="utf-8-sig")
            with (folder / "数据_00001.csv").open(encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))
            assert [float(row["CH1波形_时间(s)"]) for row in rows] == [-0.002, 0.003]
            return
        with (folder / "samples.csv").open(encoding="utf-8-sig", newline="") as file:
            rows = list(csv.DictReader(file))
        assert {r["timestamp"] for r in rows} == {batch.timestamp}
        assert rows[0]["channel_received_at"] != rows[2]["channel_received_at"]
        assert rows[0]["trigger_time_source"] == "instrument_local_timezone_unknown"
        assert rows[2]["instrument_trigger_time"] == "" and rows[2]["trigger_time_source"] == "unavailable"
    finally:
        repository.close()


def test_slow_disk_records_irregular_elapsed_times_without_retimestamping(tmp_path):
    recording = Recording(tmp_path, "electronic")
    origin = recording.started_monotonic
    entered, release = threading.Event(), threading.Event()
    append = recording.segments.append

    def slow(*args):
        entered.set()
        assert release.wait(5)
        return append(*args)

    recording.segments.append = slow
    expected = (0.217, 0.991, 1.602)
    try:
        for i, elapsed in enumerate(expected):
            assert recording.reserve(65536)
            batch = immutable_batch(timed_frame(origin + elapsed, f"2026-01-01T01:02:0{i}+00:00"))
            assert recording.submit(batch)
        assert entered.wait(2)
    finally:
        release.set()
    paths = recording.finish().result(8)
    frames = list(read_frames(Path(paths[0])))
    assert [f["timing"]["recording_elapsed_seconds"] for f in frames] == pytest.approx(expected)
    with (recording.folder / "帧索引.csv").open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    assert [float(row["距记录开始(s)"]) for row in rows] == pytest.approx(expected)
    local_column = next(key for key in rows[0] if key.startswith("本地时间"))
    assert [row[local_column] for row in rows] == [
        datetime.fromisoformat(f["timestamp"]).astimezone(recording.context.started_at.tzinfo)
        .strftime("%Y-%m-%d %H:%M:%S.%f") for f in frames
    ]


def test_legacy_csv_checkpoint_keeps_original_header(tmp_path):
    writer = SegmentWriter(tmp_path)
    path = Path(writer.append(1, replace(timed_frame(), timing={})))
    writer.close()
    # A legacy checkpoint without a version must continue the old eleven columns.
    path.with_suffix(".csv.checkpoint.json").write_text(json.dumps({"sequence": 0, "offset": 0, "frames": 0}))
    convert_csv(tmp_path)
    first = path.with_suffix(".csv").read_bytes()
    assert b"host_read_started_at" not in first.splitlines()[0]
    assert convert_csv(tmp_path)["converted"] == 1
    assert first == path.with_suffix(".csv").read_bytes()
