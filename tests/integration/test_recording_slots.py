"""Slot attribution survives all save entrypoints and offline CSV regeneration."""

import csv
import json
from pathlib import Path

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.monitoring_state import MonitorFrame
from lab_control.application.runtime import ApplicationRuntime
from lab_control.demo.instrument import SimulatedInstrument
from lab_control.domain.instruments import AcquisitionRequest, DeviceIdentity
from lab_control.storage.analysis import export_analysis, verify_analysis

from .test_monitoring_timing import wait_for


def assert_saved_slot(folder, slot, label, key):
    metadata = json.loads((folder / "_raw" / "recording.json").read_text(encoding="utf-8"))
    assert metadata["slot"] == slot and metadata["slot_label"] == label
    assert metadata["device_id"] == key and metadata["identity"]["model"] == "JW8105"
    assert f"设备槽位：{label}" in (folder / "记录说明.txt").read_text(encoding="utf-8-sig")
    tables = list(folder.glob("数据_*.csv"))
    assert tables
    for path in tables:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert rows and {row["设备槽位"] for row in rows} == {label}


@pytest.mark.parametrize("entry", ["displayed", "capture", "monitor", "direct"])
@pytest.mark.parametrize("custom", [False, True])
def test_four_identical_meters_save_distinct_slot_attribution(tmp_path, monkeypatch, entry, custom):
    monkeypatch.setattr(
        SimulatedInstrument,
        "identify",
        lambda self: DeviceIdentity("Joinwit", "JW8105", confidence="model_only"),
    )
    with ApplicationRuntime(root=tmp_path / "runtime", hardware=False) as runtime:
        runtime.monitoring.enable_settings_sync(False)
        folders = []
        request = AcquisitionRequest("scalar", channels=(1, 2), quantity="power")
        for index in range(1, 5):
            key = runtime.add(ConnectionConfig(f"mock://save-meter-{index}", "joinwit.jw8105", "mock"))
            runtime.connect(key).result(3)
            slot, label = f"optical_power_meter:{index}", f"功率计{index}"
            if custom:
                label = f"光路功率 {index}"
                slot = runtime.create_slot("optical_power_meter", label)
            runtime.bind_slot(key, slot, "general")
            service = runtime.monitoring
            if entry == "displayed":
                batch = runtime.sessions.submit(key, "acquire", request).result(3)
                folder = service.export_displayed_frame(
                    key, MonitorFrame(1, 0, request, batch), "general"
                ).result(3)
            elif entry == "capture":
                folder = service.capture_and_export(key, request, "general").result(3)
            elif entry == "monitor":
                service.start(key, (request,), interval=0.05, recording=True)
                wait_for(lambda: service.snapshots()[key]["recording_state"].get("committed", 0) > 0)
                service.stop(key).result(3)
                recorder = service.tasks[key].recorder
                recorder.finished.result(5)
                folder = recorder.folder
            else:
                paths = runtime.sessions.start_acquisition(key, request, count=1, interval=0.05).result(5)
                assert paths
                folder = runtime.sessions.sessions[key].direct_recording.folder
            folder = Path(folder)
            folders.append(folder)
            assert label in folder.name and "JW8105" in folder.name
            assert_saved_slot(folder, slot, label, key)
            if entry in {"monitor", "direct"}:
                assert verify_analysis(folder)["equal"]
        assert len(set(folders)) == 4
        # Offline conversion uses the saved label, even after a new slot binding.
        runtime.bind_slot(key, "", "general")
        runtime.delete_slot(slot)
        exported = export_analysis(folders[-1], tmp_path / "重新导出")
        assert_saved_slot(exported, slot, label, key)
