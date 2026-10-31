"""Offline processing tests never instantiate instrument transports."""

import csv
import threading
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from lab_control.application.processing import ProcessingService
from lab_control.domain.instruments import DeviceIdentity, Measurement, MeasurementBatch
from lab_control.instruments.registry import DEVICE_DEFINITIONS
from lab_control.storage import processing
from lab_control.storage.layout import atomic_json
from lab_control.storage.paths import BatchSaveContext, SaveContext
from lab_control.storage.processing import ProcessingCancelled, current_result, process_device, result_home
from lab_control.storage.processing_results import ResultReader
from lab_control.storage.recording import SegmentWriter
from lab_control.storage.recording_catalog import read_device, scan_recordings
from lab_control.storage.repository import Repository

START = datetime.fromisoformat("2026-10-31T10:00:00+08:00")
IDENTITY = DeviceIdentity("Joinwit", "JW8105", "OFFLINE-FIXTURE")


def frame(number=0, *, measurements=None, configuration=None):
    return MeasurementBatch(
        IDENTITY,
        tuple(measurements or [Measurement("power", "dBm", [-20.123456789012345 + number])]),
        configuration or {"range": 1},
        timestamp=f"2026-10-31T02:00:{number % 60:02d}+00:00",
        timing={"recording_elapsed_seconds": number * 0.125},
        settings_timestamps={"read": str(number)},
    )


def capture(root, batches=None, *, adapter="joinwit.jw8105", batch=None, layout_version=2, size=1):
    context = SaveContext(
        root,
        "general",
        "continuous",
        identity=IDENTITY,
        adapter_id=adapter,
        started_at=START,
        batch=batch,
        layout_version=layout_version,
    )
    context.prepare()
    writer = SegmentWriter(context.raw_folder, size=size)
    frames = [frame(0), frame(1), frame(2)] if batches is None else batches
    for index, item in enumerate(frames, 1):
        writer.append(index, item)
    writer.close()
    atomic_json(
        context.raw_folder / "summary.json",
        {"committed": len(frames), "binary_done": True, "analysis_state": "complete"},
    )
    return context


def processed(context):
    device = read_device(context.folder)
    folder = context.batch.folder if context.batch else context.folder
    return process_device(device, folder, threading.Event())


@pytest.fixture
def service(tmp_path, qapp):
    repo = Repository(tmp_path / "runtime")
    value = ProcessingService(repo)
    yield value
    value.close()
    repo.close()


def test_five_model_profiles_are_registered():
    profiles = {
        key: definition.offline_processing.kind
        for key, definition in DEVICE_DEFINITIONS.items()
        if definition.offline_processing
    }
    assert profiles == {
        "joinwit.jw8105": "scalar",
        "rigol.m300": "scalar",
        "kplusk.fxe": "scalar",
        "ceyear.6362d": "frames",
        "rigol.dho924": "frames",
    }


def test_catalog_batch_overlap_order_and_renamed_directory(tmp_path):
    single = capture(tmp_path)
    batch = BatchSaveContext(tmp_path, started_at=START)
    capture(tmp_path, batch=batch)
    capture(tmp_path, batch=batch, adapter="rigol.m300")
    renamed = single.folder.with_name("renamed")
    single.folder.rename(renamed)
    entries = scan_recordings((tmp_path, batch.folder, renamed))
    assert sorted((e.scope, len(e.devices)) for e in entries) == [("batch", 2), ("single_device", 1)]
    assert all(e.started == START.isoformat() for e in entries)
    assert {e.folder for e in entries} == {renamed, batch.folder}


@pytest.mark.parametrize("layout", [1, 2])
def test_exact_scalar_precision_units_timing_and_reuse(tmp_path, layout):
    context = capture(tmp_path, layout_version=layout)
    original = processing.fingerprints(read_device(context.folder), threading.Event())
    output = processed(context)
    reader = ResultReader(output)
    info = reader.info()
    assert (info["kind"], info["frames"], info["points"], info["range"]) == ("scalar", 3, 3, (0, 0.25))
    assert info["series"][0]["unit"] == "dBm"
    for i in range(1, 4):
        actual = reader.frame(i)
        np.testing.assert_array_equal(
            actual["measurements"][0]["values"], frame(i - 1).measurements[0].values
        )
        assert actual["settings_timestamps"] == {"read": str(i - 1)}
        assert actual["elapsed"] == (i - 1) * 0.125
    x, y = reader.scalar((info["series"][0]["id"],))[info["series"][0]["id"]]
    np.testing.assert_array_equal(x, [0, 0.125, 0.25])  # Readback timestamps do not create gaps.
    assert y[0] == frame().measurements[0].values[0]
    with (output / "数据_00001.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream))
    assert float(rows[1][2]) == y[0] and len(rows) == 4
    assert processed(context) == output
    assert processing.fingerprints(read_device(context.folder), threading.Event()) == original
    assert not Path(info["source"]).is_absolute()


@pytest.mark.parametrize(
    "adapter,quantity,unit,axis_unit",
    [("ceyear.6362d", "spectrum", "dBm", "nm"), ("rigol.dho924", "waveform", "V", "s")],
)
def test_random_frames_across_segments_and_channels(tmp_path, adapter, quantity, unit, axis_unit):
    samples = [
        Measurement(quantity, unit, np.arange(37, dtype=np.float32), 1, np.linspace(1, 2, 37), axis_unit),
        Measurement(quantity, unit, np.arange(19, dtype=np.float64), 2, np.linspace(1, 2, 19), axis_unit),
    ]
    context = capture(tmp_path, [frame(i, measurements=samples) for i in range(5)], adapter=adapter)
    reader = ResultReader(processed(context))
    assert reader.info()["kind"] == "frames"
    for i in (5, 1, 3):
        for expected, actual in zip(samples, reader.frame(i)["measurements"], strict=True):
            assert actual["values"].dtype == expected.values.dtype
            np.testing.assert_array_equal(actual["values"], expected.values)
            np.testing.assert_array_equal(actual["axis"], expected.axis)
    assert reader.info()["points"] == 5 * 56


@pytest.mark.parametrize("layout,npz_only", [(1, False), (2, False), (1, True)])
def test_single_snapshot_actual_shape_and_legacy_npz(tmp_path, layout, npz_only):
    repo = Repository(tmp_path / "repo")
    context = SaveContext(
        tmp_path / "captures",
        "general",
        "single_frame",
        identity=IDENTITY,
        adapter_id="joinwit.jw8105",
        layout_version=layout,
    )
    try:
        repo.export(frame(), context=context)
        if npz_only:
            (context.raw_folder / "recording.json").unlink()
            (context.raw_folder / "snapshot.json").unlink()
        entries = scan_recordings((context.root,))
        assert len(entries) == 1
        reader = ResultReader(processed(context))
        assert reader.info()["kind"] == "scalar" and reader.info()["frames"] == 1
    finally:
        repo.close()


def test_missing_channel_and_configuration_changes_are_gaps(tmp_path):
    both = [Measurement("power", "dBm", [-2], 1), Measurement("power", "dBm", [-3], 2)]
    context = capture(
        tmp_path,
        [
            frame(0, measurements=both),
            frame(1),
            frame(2, measurements=both),
            frame(3, measurements=both, configuration={"range": 2}),
        ],
    )
    reader = ResultReader(processed(context))
    series = {s["channel"]: s["id"] for s in reader.info()["series"]}
    data = reader.scalar(tuple(series.values()))
    assert np.isnan(data[series[1]][0]).sum() == 1
    assert np.isnan(data[series[2]][0]).sum() == 2


def test_envelope_stays_bounded_with_frequent_configuration_changes(tmp_path):
    context = capture(tmp_path, [frame(i, configuration={"range": i}) for i in range(500)], size=10**9)
    reader = ResultReader(processed(context))
    key = reader.info()["series"][0]["id"]
    x, y = reader.scalar((key,), points=80)[key]
    assert len(x) <= 88 and np.isfinite(y).sum() > 1
    assert x[0] == 0 and x[-1] == 499 * 0.125
    assert np.isnan(x).any()


@pytest.mark.parametrize(
    "damage", ["missing_segment", "bad_sqlite", "missing_config", "wrong_count", "missing_snapshot"]
)
def test_corruption_keeps_prior_complete_result(tmp_path, damage):
    context = capture(tmp_path)
    previous = processed(context)
    device = read_device(context.folder)
    segments = sorted(context.raw_folder.glob("segment-*.sqlite3"))
    if damage == "missing_segment":
        segments[1].unlink()
    elif damage == "bad_sqlite":
        segments[1].write_text("broken")
    elif damage == "missing_config":
        import sqlite3

        with sqlite3.connect(segments[1]) as db:
            db.execute("DELETE FROM configs")
        db.close()
    elif damage == "wrong_count":
        atomic_json(context.raw_folder / "summary.json", {"committed": 9})
    else:
        for segment in segments:
            segment.unlink()
    with pytest.raises(Exception):
        processed(context)
    assert current_result(result_home(context.folder, device)) == previous
    assert not list(previous.parent.glob(".processing-*"))


def test_empty_unknown_bad_manifest_and_cancelled_rebuild(tmp_path, monkeypatch):
    empty = capture(tmp_path, [])
    assert ResultReader(processed(empty)).info()["frames"] == 0
    unknown = capture(tmp_path, adapter="unknown")
    with pytest.raises(ValueError, match="不支持"):
        processed(unknown)
    context = capture(tmp_path)
    old = processed(context)
    cancel = threading.Event()
    monkeypatch.setattr(processing, "PROCESSING_VERSION", 2)
    original = processing.ProcessedWriter.append

    def interrupted(self, frame):
        original(self, frame)
        cancel.set()

    monkeypatch.setattr(processing.ProcessedWriter, "append", interrupted)
    with pytest.raises(ProcessingCancelled):
        process_device(read_device(context.folder), context.folder, cancel)
    assert current_result(old.parent) == old
    (context.raw_folder / "recording.json").write_text("not json")
    entries = scan_recordings((context.root,))
    assert any(d.error for e in entries for d in e.devices)


def test_batch_failure_does_not_stop_next_device(service, tmp_path):
    batch = BatchSaveContext(tmp_path / "data", started_at=START)
    capture(batch.root, batch=batch, adapter="unknown")
    capture(batch.root, batch=batch)
    entry = scan_recordings((batch.root,))[0]
    outcomes = service.process(entry).result(10)
    assert len(outcomes) == 2 and sum(bool(result["result"]) for result in outcomes) == 1


def test_corrupt_summary_fails_only_its_device_in_batch(service, tmp_path):
    batch = BatchSaveContext(tmp_path / "data", started_at=START)
    broken = capture(batch.root, batch=batch)
    capture(batch.root, batch=batch)
    (broken.raw_folder / "summary.json").write_text("broken")
    (entry,) = scan_recordings((batch.root,))
    outcomes = service.process(entry).result(10)
    assert sum(bool(result["error"]) for result in outcomes) == 1
    assert sum(bool(result["result"]) for result in outcomes) == 1


def test_move_rename_restart_and_result_relative_paths(service, tmp_path):
    context = capture(tmp_path / "source")
    old_result = processed(context)
    service.catalog.add_root(context.root)
    entry = service.catalog.scan()[0]
    moved = service.rename(entry, "new name").result(10)
    assert not entry.folder.exists()
    entry = service.catalog.scan()[0]
    assert entry.folder == Path(moved["destination"]) and entry.started == START.isoformat()
    parent = tmp_path / "destination"
    parent.mkdir()
    service.move(entry, parent).result(10)
    service.close()
    restarted = ProcessingService(service.repository)
    try:
        entry = restarted.catalog.scan()[0]
        assert entry.folder == parent / "new name"
        result = current_result(result_home(entry.folder, entry.devices[0]))
        assert result.name == old_result.name
        assert ResultReader(result).info()["frames"] == 3
        source = (result.parent / ResultReader(result).metadata["source"]).resolve()
        assert source == entry.folder
    finally:
        restarted.close()


@pytest.mark.parametrize("state", [{"binary_done": False}, {"analysis_state": "generating"}])
def test_active_record_guards(service, tmp_path, state):
    context = capture(tmp_path)
    entry = scan_recordings((context.root,))[0]
    atomic_json(context.raw_folder / "summary.json", state)
    with pytest.raises(ValueError, match="收尾"):
        service.rename(entry, "no")


def test_recheck_active_before_execution_collision_and_trash_failure(service, tmp_path, monkeypatch):
    from PySide6.QtCore import QFile

    context = capture(tmp_path)
    entry = scan_recordings((context.root,))[0]
    (entry.folder.parent / "taken").mkdir()
    with pytest.raises(ValueError, match="目标已存在"):
        service.rename(entry, "taken").result(10)
    monkeypatch.setattr(QFile, "moveToTrash", lambda _path: (False, ""))
    with pytest.raises(OSError, match="回收站"):
        service.trash(entry).result(10)
    assert entry.folder.exists()
    release = threading.Event()
    service.worker.submit(lambda: release.wait(5))
    future = service.rename(entry, "queued")
    service.active = lambda _path: True
    release.set()
    with pytest.raises(ValueError, match="采集"):
        future.result(10)
    assert entry.folder.exists()


@pytest.mark.parametrize("failure", ["none", "copy", "verify", "remove"])
def test_cross_volume_move_checks_copy_and_recovers_failures(service, tmp_path, monkeypatch, failure):
    import shutil

    context = capture(tmp_path / "source")
    processed(context)
    service.catalog.add_root(context.root)
    entry = service.catalog.scan()[0]
    parent = tmp_path / "target"
    parent.mkdir()
    before = service._signature(entry.folder)
    monkeypatch.setattr(service, "_same_volume", lambda *_: False)
    if failure == "copy":
        monkeypatch.setattr(shutil, "copytree", lambda *_: (_ for _ in ()).throw(OSError("copy failed")))
    if failure == "verify":
        original_signature = service._signature
        monkeypatch.setattr(
            service,
            "_signature",
            lambda folder: {} if folder.name.startswith(".processing-move-") else original_signature(folder),
        )
    if failure == "remove":
        original_remove = shutil.rmtree

        def fail_remove(path, *args, **kwargs):
            if Path(path) == entry.folder:
                raise OSError("source removal failed")
            return original_remove(path, *args, **kwargs)

        monkeypatch.setattr(shutil, "rmtree", fail_remove)
    future = service.move(entry, parent)
    if failure == "none":
        future.result(10)
        assert not entry.folder.exists()
        assert service._signature(parent / entry.name) == before
    else:
        with pytest.raises(OSError):
            future.result(10)
        assert service._signature(entry.folder) == before
        assert (parent / entry.name).exists() == (failure == "remove")
    if failure in {"none", "remove"}:
        service.repository.save_workspace("processing/roots", [])
        # Restart discovery also consults journals; no deletion is replayed.
        assert parent / entry.name in {e.folder for e in service.catalog.scan()}
    assert not list(parent.glob(".processing-move-*"))


def test_finalized_wal_is_read_on_copy_without_modifying_sources(tmp_path):
    import shutil

    context = capture(tmp_path, [frame(0)])
    segment = next(context.raw_folder.glob("*.sqlite3"))
    # Keep a writer alive only to create a realistic committed WAL fixture;
    # processing is pointed at its isolated frozen copy with a completed summary.
    live = SegmentWriter(tmp_path / "live")
    live.append(1, frame(7))
    try:
        source = Path(live.paths[0])
        shutil.copyfile(source, segment)
        shutil.copyfile(Path(str(source) + "-wal"), Path(str(segment) + "-wal"))
        before = processing.fingerprints(read_device(context.folder), threading.Event())
        result = processed(context)
        assert ResultReader(result).frame(1)["elapsed"] == 0.875
        assert processing.fingerprints(read_device(context.folder), threading.Event()) == before
        assert not Path(str(segment) + "-shm").exists()
    finally:
        live.close()


def test_corrupt_batch_member_and_child_root_stay_in_whole_batch(tmp_path):
    batch = BatchSaveContext(tmp_path, started_at=START)
    first = capture(tmp_path, batch=batch)
    second = capture(tmp_path, batch=batch)
    (second.raw_folder / "recording.json").write_text("broken")
    (entry,) = scan_recordings((first.folder,))
    assert entry.scope == "batch" and entry.folder == batch.folder and len(entry.devices) == 2
    assert sum(bool(d.error) for d in entry.devices) == 1


def test_source_change_during_processing_does_not_publish(tmp_path, monkeypatch):
    context = capture(tmp_path)
    original = processing.ProcessedWriter.append

    def change_source(self, frame):
        original(self, frame)
        with (context.raw_folder / "acquisition-events.jsonl").open("a") as stream:
            stream.write("{}\n")

    monkeypatch.setattr(processing.ProcessedWriter, "append", change_source)
    with pytest.raises(ValueError, match="数据变化"):
        processed(context)
    assert current_result(result_home(context.folder, read_device(context.folder))) is None


@pytest.mark.parametrize("damage", ["cache", "csv", "pointer"])
def test_reprocessing_repairs_broken_outputs_without_changing_sources(tmp_path, damage):
    context = capture(tmp_path)
    previous = processed(context)
    source = processing.fingerprints(read_device(context.folder), threading.Event())
    if damage == "cache":
        (previous / "data.sqlite3").write_bytes(b"broken")
    elif damage == "csv":
        next(previous.glob("数据_*.csv")).unlink()
    else:
        (previous.parent / "current.json").write_text("[]")
    rebuilt = processed(context)
    assert previous != rebuilt and previous.exists()
    assert ResultReader(rebuilt).info()["frames"] == 3
    assert processing.fingerprints(read_device(context.folder), threading.Event()) == source


@pytest.mark.parametrize("tuple_result", [False, True])
def test_recycle_success_delegates_whole_directory_without_delete_fallback(
    service, tmp_path, monkeypatch, tuple_result
):
    from PySide6.QtCore import QFile

    context = capture(tmp_path)
    processed(context)
    (entry,) = scan_recordings((context.root,))
    recycle = tmp_path / "isolated-trash"

    def trash(path):
        assert Path(path) == entry.folder
        Path(path).rename(recycle)
        return (True, str(recycle)) if tuple_result else True

    monkeypatch.setattr(QFile, "moveToTrash", trash)
    assert service.trash(entry).result(10)["trash"] == (str(recycle) if tuple_result else "")
    assert (recycle / "数据处理").is_dir() and (recycle / "_raw").is_dir()
