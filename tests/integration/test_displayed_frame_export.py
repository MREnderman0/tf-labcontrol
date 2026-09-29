import json
import threading
from dataclasses import replace

import numpy as np
import pytest

from lab_control.application.acquisition_pipeline import validate_recorded_batch
from lab_control.application.monitoring import MonitorFrame
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError

from .test_monitoring_timing import connected, wait_for


@pytest.mark.parametrize("validation", ["live", "cached", "full"])
def test_display_snapshot_preserves_data_and_provenance_without_io(tmp_path, validation):
    runtime, key = connected(tmp_path)
    with runtime:
        service = runtime.monitoring
        service.start(key, (AcquisitionRequest("spectrum"),), once=True)
        wait_for(lambda: service.latest(key, "spectrum") is not None)
        service.stop(key).result(3)
        frame = service.latest(key, "spectrum")
        batch = replace(
            frame.batch, validation=validation, settings_timestamps={"span": "original-read-time"}
        )
        frame = replace(frame, batch=batch)
        if validation != "full":
            with pytest.raises(DeviceError):
                validate_recorded_batch(batch)
        adapter = runtime.sessions.sessions[key].adapter

        def forbidden(*args):
            pytest.fail("Snapshot export accessed the instrument")

        adapter.acquire = adapter.apply = adapter.read_settings = forbidden
        folder = service.export_displayed_frame(key, frame, "electronic").result(3)
        saved = json.loads((folder / "_raw" / "snapshot.json").read_text(encoding="utf-8"))
        assert saved["timestamp"] == batch.timestamp and saved["validation"] == validation
        assert saved["settings_timestamps"] == batch.settings_timestamps
        assert saved["configuration"] == batch.configuration
        with np.load(folder / "_raw" / "samples.npz") as arrays:
            np.testing.assert_array_equal(arrays["values_0"], batch.measurements[0].values)
            np.testing.assert_array_equal(arrays["axis_0"], batch.measurements[0].axis)


def test_slow_snapshot_export_does_not_block_reading_or_controls(tmp_path):
    runtime, key = connected(tmp_path)
    with runtime:
        service = runtime.monitoring
        service.start(key, (AcquisitionRequest("spectrum"),), cadence="fastest")
        wait_for(lambda: service.latest(key, "spectrum") is not None)
        frame = service.latest(key, "spectrum")
        entered, release = threading.Event(), threading.Event()
        calls = []

        def write(batch, mode, **options):
            calls.append(batch)
            entered.set()
            assert release.wait(5)
            raise OSError("disk full")

        runtime.repository.export = write
        future = service.export_displayed_frame(key, frame, "electronic")
        try:
            assert entered.wait(2)
            with pytest.raises(DeviceError, match="正在保存"):
                service.export_displayed_frame(key, frame, "electronic")
            before = service.snapshots()[key]["sequence"]
            wait_for(lambda: service.snapshots()[key]["sequence"] > before + 3)
            assert not runtime.sessions.snapshots()[0]["controlsLocked"]
            assert (
                service.apply(key, DeviceCommand("center_frequency", {"value": 2e6})).result(3).outcome
                == "success"
            )
        finally:
            release.set()
        with pytest.raises(OSError, match="disk full"):
            future.result(3)
        wait_for(lambda: not service.snapshots()[key]["display_saving"])
        service.stop(key).result(3)
        assert calls == [frame.batch]


def test_close_drains_display_export_and_rejects_invalid_samples(tmp_path):
    runtime, key = connected(tmp_path)
    adapter = runtime.sessions.sessions[key].adapter
    request = AcquisitionRequest("spectrum")
    batch = adapter.acquire(request)
    bad = replace(batch, measurements=(replace(batch.measurements[0], values=np.array([np.nan])),))
    with pytest.raises(DeviceError):
        runtime.monitoring.export_displayed_frame(key, MonitorFrame(1, 0, request, bad), "electronic")
    future = runtime.monitoring.export_displayed_frame(key, MonitorFrame(1, 0, request, batch), "electronic")
    runtime.close()
    assert future.done() and (future.result() / "数据_00001.csv").exists()
