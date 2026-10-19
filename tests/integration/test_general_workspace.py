import json

import pytest
from PySide6.QtWidgets import QLabel

from lab_control.application.catalog import instrument_category
from lab_control.application.config import config_from_record
from lab_control.application.controls import data_sources
from lab_control.demo.workspace import create_demo_runtime
from lab_control.domain.instruments import DeviceCommand
from lab_control.storage.repository import Repository
from lab_control.storage.workspace_migration import MIGRATION_KEY
from lab_control.ui.real_window import RealWindow


@pytest.mark.parametrize("old_mode", [None, "electronic", "optical"])
def test_startup_and_mixed_device_monitoring_record_in_general_workspace(qtbot, tmp_path, old_mode):
    if old_mode:
        repo = Repository(tmp_path / "simulation")
        repo.save_workspace("current_mode", old_mode)
        repo.close()
    runtime = create_demo_runtime(tmp_path)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    try:
        assert window.controller.mode == "general"
        assert not hasattr(window, "mode") and not hasattr(window, "mode_badge")
        assert not any(
            label.text() in {"电子学模式", "光学模式", "实验模式"} for label in window.findChildren(QLabel)
        )
        records = runtime.inventory.all()
        categories = (
            "signal_generator",
            "power_supply",
            "oscilloscope",
            "spectrum_analyzer",
            "optical_power_meter",
            "optical_spectrometer",
        )
        selected = {
            category: next(r for r in records if instrument_category(r) == category)
            for category in categories
        }
        source_id = selected["signal_generator"]["id"]
        window.navigate("设备管理", "signal_generator", source_id)
        page = window.stack.currentWidget()
        assert page.tabs.count() == 0 and page.tabs.isHidden()
        assert page.settings_button.isVisible()
        assert set(page.panels[1].editors) == {"waveform", "frequency", "amplitude", "offset", "phase"}
        qtbot.waitUntil(lambda: not window.controller.group_devices)
        applied = []
        window.controller.apply(source_id, DeviceCommand("frequency", {"value": 2000}), applied.append)
        qtbot.waitUntil(lambda: bool(applied), timeout=5000)
        assert applied[0].outcome == "success"
        runtime.monitoring.enable_settings_sync(False)
        qtbot.waitUntil(lambda: all(not session.pending for session in runtime.sessions.sessions.values()))
        for category, record in selected.items():
            window.controller.ensure_applicable(record["id"])
            source = data_sources(config_from_record(record))[0]
            request = source.request(
                (1,), points=32, initiate=category == "optical_spectrometer", confirmed=True
            )
            with runtime.monitoring.lock:
                runtime.monitoring.start(
                    record["id"], (request,), interval=0.5, cadence="interval", record_interval=None
                )
                runtime.monitoring.set_recording(record["id"], True)
        qtbot.waitUntil(
            lambda: all(runtime.monitoring.tasks[r["id"]].sequence >= 2 for r in selected.values()),
            timeout=12000,
        )
        futures = [runtime.monitoring.stop(r["id"]) for r in selected.values()]
        for future in futures:
            future.result(15)
        for record in selected.values():
            task = runtime.monitoring.tasks[record["id"]]
            assert task.mode == "general" and not task.error and not task.pending
        metadata = list(runtime.repository.data_root.rglob("recording.json"))
        assert len(metadata) == len(selected)
        assert all(json.loads(path.read_text(encoding="utf-8"))["mode"] == "general" for path in metadata)
    finally:
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.closing = True
        window.bridge.worker.shutdown()


def test_migrated_slot_conflicts_remain_visible_and_devices_can_be_rebound(qtbot, tmp_path):
    runtime = create_demo_runtime(tmp_path)
    source_ids = [r["id"] for r in runtime.inventory.all() if instrument_category(r) == "signal_generator"]
    runtime.close()
    repo = Repository(tmp_path / "simulation")
    try:
        for record in repo.records():
            if record["id"] in source_ids:
                mode = "electronic" if record["id"] == source_ids[0] else "optical"
                record["slots"] = {mode: "signal_generator:1"}
                repo.save(record)
        with repo.db:
            repo.db.execute(
                "DELETE FROM workspace WHERE id IN (?, ?)", (MIGRATION_KEY, "demo_general_slots_initialized")
            )
    finally:
        repo.close()
    runtime = create_demo_runtime(tmp_path)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    try:
        window.open_category("signal_generator")
        runtime.monitoring.enable_settings_sync(False)
        qtbot.waitUntil(lambda: all(not session.pending for session in runtime.sessions.sessions.values()))
        for device_id in source_ids:
            window.controller.ensure_applicable(device_id)
            assert window.controller.records[device_id]["slots"]["general"] == ""
            assert "重新绑定" in window.rows[device_id].text(4)
            assert runtime.sessions.sessions[device_id].state == "connected"
        first, second = source_ids
        runtime.bind_slot(first, "signal_generator:2", "general")
        runtime.bind_slot(second, "", "general")
        qtbot.waitUntil(lambda: not any(window.controller.slot_notice(key) for key in source_ids))
        window.refresh()
        assert not window.controller.slot_notice(first)
        assert not window.controller.slot_notice(second)
        assert "重新绑定" not in window.rows[first].text(4)
        records = {r["id"]: r for r in runtime.repository.records()}
        assert records[first]["slots"] == {
            "electronic": "signal_generator:1",
            "general": "signal_generator:2",
        }
        assert records[second]["slots"] == {"optical": "signal_generator:1", "general": ""}
    finally:
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.closing = True
        window.bridge.worker.shutdown()
