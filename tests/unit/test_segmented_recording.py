import csv
import json
import threading
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from lab_control.application.acquisition_pipeline import immutable_batch
from lab_control.domain.instruments import DeviceIdentity, Measurement, MeasurementBatch
from lab_control.storage.recording import Recording, SegmentWriter, convert_csv, read_frames


def frame(dtype="<f4"):
    return MeasurementBatch(
        DeviceIdentity("Fixture", "Recorder"),
        (
            Measurement(
                "spectrum",
                "dBm",
                np.arange(8, dtype=dtype).reshape(2, 4),
                axis=np.arange(8, dtype=">f8").reshape(2, 4),
                axis_unit="Hz",
            ),
        ),
        {"span": 8, "nested": {"values": [1, 2]}},
        validation="full",
        settings_timestamps={"span": "startup-time"},
    )


@pytest.mark.parametrize("dtype", ["<f4", ">f4", "<f8", "<i2", "<c16"])
def test_segments_preserve_values_dtype_shape_metadata_and_csv(tmp_path, dtype):
    batch = frame(dtype)
    writer = SegmentWriter(tmp_path, size=1)
    paths = [Path(writer.append(i, batch)) for i in range(1, 4)]
    writer.close()
    assert len(set(paths)) == 3
    for path in paths:
        saved = next(read_frames(path))
        actual = saved["measurements"][0]
        assert actual["values"].dtype.str == dtype
        assert actual["values"].shape == (2, 4)
        assert np.array_equal(actual["values"], batch.measurements[0].values)
        assert actual["axis"].dtype.str == ">f8"
        assert saved["configuration"] == batch.configuration
        assert saved["settings_timestamps"] == batch.settings_timestamps
    assert convert_csv(tmp_path)["converted"] == 3
    assert convert_csv(tmp_path)["converted"] == 3
    for path in paths:
        with path.with_suffix(".csv").open(encoding="utf-8", newline="") as file:
            rows = list(csv.DictReader(file))
        assert len(rows) == 8
        assert [float(row["real"]) for row in rows] == list(range(8))


def test_csv_recovers_tail_and_interruption_before_checkpoint(tmp_path, monkeypatch):
    writer = SegmentWriter(tmp_path)
    path = Path(writer.append(1, frame()))
    writer.close()
    import lab_control.storage.recording as module

    atomic = module._atomic_json

    def crash(path, value):
        if "checkpoint" in path.name:
            raise OSError("power loss before checkpoint")
        return atomic(path, value)

    monkeypatch.setattr(module, "_atomic_json", crash)
    with pytest.raises(OSError, match="power loss"):
        convert_csv(tmp_path)
    monkeypatch.setattr(module, "_atomic_json", atomic)
    convert_csv(tmp_path)
    csv_path = path.with_suffix(".csv")
    original = csv_path.read_bytes()
    with csv_path.open("ab") as output:
        output.write(b"half-frame,invalid-tail")
    convert_csv(tmp_path)
    assert csv_path.read_bytes() == original
    assert len(list(csv.DictReader(original.decode().splitlines()))) == 8


def test_partial_sqlite_frame_rolls_back_without_export(tmp_path):
    writer = SegmentWriter(tmp_path)
    batch = frame()
    bad = replace(batch, measurements=(replace(batch.measurements[0], axis=np.arange(2)),))
    with pytest.raises(Exception, match="Axis/data"):
        writer.append(1, bad)
    writer.close()
    assert not list(read_frames(Path(writer.paths[0])))
    assert convert_csv(tmp_path)["converted"] == 0


def test_publication_owns_immutable_arrays_and_nested_metadata():
    source = frame()
    published = immutable_batch(source)
    source.measurements[0].values[0, 0] = 99
    source.configuration["nested"]["values"].append(3)
    assert published.measurements[0].values[0, 0] == 0
    assert published.configuration["nested"]["values"] == (1, 2)
    with pytest.raises(ValueError):
        published.measurements[0].values.setflags(write=True)
    with pytest.raises(TypeError):
        published.configuration["span"] = 9


def test_bounded_buffer_preserves_accepted_frames_and_drains_on_pause(tmp_path):
    recording = Recording(tmp_path, "electronic", max_frames=2)
    entered, release = threading.Event(), threading.Event()
    append = recording.segments.append

    def slow(sequence, batch):
        entered.set()
        assert release.wait(5)
        return append(sequence, batch)

    recording.segments.append = slow
    try:
        assert recording.reserve(8192) and recording.submit(immutable_batch(frame()))
        assert entered.wait(2)
        assert recording.reserve(8192) and recording.submit(immutable_batch(frame()))
        assert not recording.reserve(8192)
        state = recording.snapshot()
        assert state["received"] == state["pending"] == 2 and state["paused"]
    finally:
        release.set()
    paths = recording.finish().result(8)
    assert recording.snapshot()["committed"] == recording.snapshot()["converted"] == 2
    assert len(list(read_frames(Path(paths[0])))) == 2


def test_disk_failure_is_not_retried_and_preserves_uncommitted_count(tmp_path):
    recording = Recording(tmp_path, "electronic")
    calls = []

    def fail(*args):
        calls.append(args[0])
        raise OSError("disk unavailable")

    recording.segments.append = fail
    assert recording.reserve(8192)
    recording.submit(immutable_batch(frame()))
    recording.finish().result(5)
    assert calls == [1]
    state = recording.snapshot()
    assert state["received"] == state["pending"] == 1 and not state["committed"]
    assert "disk unavailable" in state["error"]


def test_stop_waits_for_reserved_frame_and_csv_failure_keeps_binary(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("CSV executable unavailable")

    monkeypatch.setattr("lab_control.storage.recording.subprocess.Popen", fail)
    recording = Recording(tmp_path, "electronic")
    assert recording.reserve(8192)
    future = recording.finish()
    assert not future.done() and not recording.binary_done
    assert recording.submit(immutable_batch(frame()))
    paths = future.result(5)
    assert len(list(read_frames(Path(paths[0])))) == 1
    assert "CSV executable unavailable" in recording.snapshot()["csv_error"]
    assert convert_csv(recording.folder)["converted"] == 1


def test_final_summary_and_segment_config_dedup(tmp_path):
    recording = Recording(tmp_path, "optical")
    for _ in range(3):
        assert recording.reserve(8192)
        recording.submit(immutable_batch(frame()))
    paths = recording.finish().result(8)
    summary = json.loads((recording.folder / "summary.json").read_text(encoding="utf-8"))
    assert summary["received"] == summary["committed"] == summary["converted"] == 3
    assert summary["version"] == 1 and summary["segments"] == paths
    assert len({f["config_id"] for f in read_frames(Path(paths[0]))}) == 1


def test_byte_reservation_rejects_before_accepting_frame(tmp_path):
    recording = Recording(tmp_path, "electronic", max_bytes=1024)
    assert not recording.reserve(2048)
    assert recording.finish().result(3) == []
    assert recording.snapshot()["received"] == 0 and recording.snapshot()["paused"]


def test_failed_binary_close_always_finishes_and_reports_error(tmp_path):
    recording = Recording(tmp_path, "electronic")

    def fail():
        raise OSError("close failed")

    recording.segments.close = fail
    assert recording.finish().result(3) == []
    assert recording.binary_done and "close failed" in recording.snapshot()["error"]
