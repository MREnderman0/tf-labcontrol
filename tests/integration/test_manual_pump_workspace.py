import json

from PySide6.QtWidgets import QMessageBox, QPushButton
from pytest import raises

from lab_control.application.runtime import ApplicationRuntime
from lab_control.discovery.providers import ConfiguredDiscoveryProvider, SerialDiscoveryProvider
from lab_control.discovery.types import DiscoveryCandidate
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import ValidationError
from lab_control.instruments.devices.custom_optical_comb_pump.adapter import ADAPTER_ID, OUTPUT_OFF
from lab_control.instruments.devices.custom_optical_comb_pump.hardware.readonly import verify
from lab_control.transport.visa import VisaRuntime
from lab_control.ui.real_window import RealWindow

PORT = "COM99"
RESOURCE = "ASRL99::INSTR"
SETTINGS_REQUEST = bytes.fromhex("48 F1 00 00 00 00 01 53")
OFF_REQUEST = bytes.fromhex("48 F2 00 00 00 00 01 53")
CURRENT_REQUEST = bytes.fromhex("48 04 00 00 00 00 01 53")


class FakeVisaSerial:
    def __init__(self):
        self.output = 1
        self.writes = []
        self.reply = b""
        self.closed = False
        self.fail_current = False

    def write_raw(self, request):
        assert self.baud_rate == 115200 and self.data_bits == 8
        assert self.read_termination is None and self.write_termination is None
        assert not self.reply and len(request) == 8
        self.writes.append(request)
        if request == SETTINGS_REQUEST:
            self.reply = (
                bytes.fromhex("48 01 00 0F 42 40 01 53 48 12 00 00 5B 68 01 53")
                + bytes([0x48, 0xF2, 0, 0, 0, self.output, 1, 0x53])
                + bytes.fromhex("48 0A 00 00 07 D0 01 53 48 F3 00 00 00 00 01 53 48 11 00 0F 42 40 01 53")
            )
        elif request == OFF_REQUEST:
            self.output = 0
            self.reply = request
        elif request == CURRENT_REQUEST:
            self.reply = b"" if self.fail_current else bytes.fromhex("48 04 00 00 00 00 01 53")
        else:
            raise AssertionError(request)

    def read_bytes(self, count, *, break_on_termchar):
        assert not break_on_termchar
        result, self.reply = self.reply[:count], self.reply[count:]
        return result

    def close(self):
        self.closed = True


class FakeVisaManager:
    def __init__(self):
        self.opens = []
        self.handle = FakeVisaSerial()

    def open_resource(self, resource, **_kwargs):
        self.opens.append(resource)
        assert resource == RESOURCE
        return self.handle

    def close(self):
        pass


def test_manual_pump_connection_control_and_monitoring_use_selected_port_only(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr("lab_control.storage.runtime.hardware_lock_home", lambda: tmp_path / "locks")
    monkeypatch.setattr("lab_control.storage.repository.application_home", lambda: tmp_path / "home")
    port = DiscoveryCandidate(ConnectionConfig(PORT, transport="serial"), "serial")
    monkeypatch.setattr(SerialDiscoveryProvider, "enumerate", lambda self: (port,))
    monkeypatch.setattr(
        "lab_control.ui.workspace.dialogs.choose_item",
        lambda _parent, _title, _label, items: (items[0], True),
    )
    manager = FakeVisaManager()
    with ApplicationRuntime(root=tmp_path, visa=VisaRuntime(manager=manager)) as runtime:
        config = ConnectionConfig(RESOURCE, ADAPTER_ID)
        device_id = runtime.add(config)
        providers = (ConfiguredDiscoveryProvider((config,)), SerialDiscoveryProvider())
        for _ in range(2):
            scan = runtime.discovery.scan(providers).result(5)
            assert scan["items"][0]["reason"] == "manual_connection_required"
        assert len(runtime.inventory.all()) == 1
        with raises(ValidationError, match="手动选择"):
            runtime.connect(device_id)
        assert runtime.connect_all_discovered().result(5)["total"] == 0
        assert not manager.opens

        window = RealWindow(runtime)
        qtbot.addWidget(window)
        window.show()
        try:
            button = next(
                widget for widget in window.findChildren(QPushButton) if widget.text() == "手动连接泵浦"
            )
            button.click()
            qtbot.waitUntil(lambda: ("设备管理", "pump_driver") in window.page_map)
            page = window.page_map[("设备管理", "pump_driver")]
            assert page.selector.device_id == device_id
            assert manager.opens == [RESOURCE] and manager.handle.writes == [SETTINGS_REQUEST]
            assert runtime.inventory.all()[0]["identityKey"] is None
            assert runtime.connect_all_discovered().result(5)["total"] == 0

            off = page.form.editors[OUTPUT_OFF]
            assert off.apply_button.isEnabled() and not off.read_button.isVisible()
            assert not page.form.editors["output"].cap.writable
            monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
            off.apply_button.click()
            assert manager.handle.writes == [SETTINGS_REQUEST]
            monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
            off.apply_button.click()
            qtbot.waitUntil(lambda: "已关闭" in off.readback.text())
            assert manager.handle.writes == [SETTINGS_REQUEST, OFF_REQUEST, SETTINGS_REQUEST]

            window.navigate("实时监测", "pump_driver", device_id)
            monitor = window.stack.currentWidget()
            assert len(monitor.sources) == 7 and not monitor.initiate.isEnabled()
            monitor.start(once=True)
            qtbot.waitUntil(lambda: runtime.monitoring.latest(device_id, "scalar") is not None)
            frame = runtime.monitoring.latest(device_id, "scalar")
            assert frame.batch.measurements[0].values == [0.0]
            assert manager.handle.writes[-1] == CURRENT_REQUEST
            manager.handle.fail_current = True
            monitor.start()
            qtbot.waitUntil(lambda: bool(runtime.monitoring.snapshots()[device_id]["error"]))
            snapshot = runtime.monitoring.snapshots()[device_id]
            assert not snapshot["running"] and not snapshot["recovering"]
            assert manager.handle.closed and manager.opens == [RESOURCE]
        finally:
            window.timer.stop()
            window.controller.close()
            window.closing = True
            window.bridge.worker.shutdown()
    assert manager.handle.closed


def test_readonly_hardware_entry_records_blocked_enumeration_without_opening_a_session(tmp_path, monkeypatch):
    monkeypatch.setattr(SerialDiscoveryProvider, "enumerate", lambda self: ())
    folder = tmp_path / "hardware-tests" / "fixture-pump" / "batch-1"
    report = verify(
        ConnectionConfig(RESOURCE, ADAPTER_ID), folder, duration=1, root=tmp_path, input_condition="fixture"
    )
    assert not report["checks_passed"] and report["stages"]["enumeration"]["state"] == "blocked"
    assert report["stages"]["compatibility_connection"]["state"] == "not_tested"
    assert (folder / "communication.jsonl").read_bytes() == b""
    summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    assert not summary["identity_verified"] and summary["adapter_id"] == ADAPTER_ID
    assert (folder.parent.parent / "index.md").exists()
