from dataclasses import asdict
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QListWidget, QPushButton, QWidget

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceCommand, ValidationError
from lab_control.instruments.registry import build_scpi_adapter
from lab_control.transport.base import MockTransport
from lab_control.ui.real_window import RealWindow
from lab_control.ui.workspace.dialogs import ConnectionDialogs


@pytest.mark.parametrize("model,transport", [("itech.it6322", "tcp"), ("microchip.53100a", "tserve")])
def test_noop_adapter_form_preserves_hidden_connection_fields_and_limits(
    qtbot, monkeypatch, model, transport
):
    current = ConnectionConfig(
        "tcp://192.0.2.20:5025",
        model,
        transport,
        limits={"voltage": 1.0},
        sdk_path="fixture-sdk",
        usb_instance_id="fixture-usb",
        server_path="fixture-server",
        data_resource="fixture-data",
        stream_format="F",
        options=("fixture",),
        aliases=("fixture-alias",),
        terminator="\r\n",
        source_address="192.0.2.30" if transport == "tcp" else "",
    )
    saved = []
    window = QWidget()
    qtbot.addWidget(window)
    window.records = {"fixture": {"config": asdict(current)}}
    window.runtime = SimpleNamespace(configure_device=lambda key, config: saved.append(config))
    window.refresh = lambda: None
    window._failed = pytest.fail
    monkeypatch.setattr(QDialog, "exec", lambda self: 1)
    ConnectionDialogs(window).configure_adapter("fixture")
    assert saved == [current]
    if model == "itech.it6322":
        wire = MockTransport([])
        adapter = build_scpi_adapter(saved[0], wire)
        with pytest.raises(ValidationError, match="limit"):
            adapter.validate_command(DeviceCommand("voltage", {"value": 2.0}, channel=1))
        assert not wire.history


def test_address_selection_survives_refresh_and_drives_selected_actions(qtbot, tmp_path, monkeypatch):
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    first = runtime.static_addresses.add("192.0.2.1")
    second = runtime.static_addresses.add("192.0.2.2")
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.timer.stop()
    window.controller.timer.stop()
    scanned = []
    from concurrent.futures import Future

    def scan(ids):
        scanned.append(ids)
        return Future()

    monkeypatch.setattr(runtime, "scan_static_addresses", scan)

    def interact():
        dialog = QApplication.activeModalWidget()
        try:
            listing = dialog.findChild(QListWidget, "staticAddressList")
            item = next(
                listing.item(i)
                for i in range(listing.count())
                if listing.item(i).data(Qt.ItemDataRole.UserRole) == first.id
            )
            item.setSelected(True)
            window._refresh_static_address_dialog()
            assert [i.data(Qt.ItemDataRole.UserRole) for i in listing.selectedItems()] == [first.id]
            buttons = {b.text(): b for b in dialog.findChildren(QPushButton)}
            assert buttons["扫描选中"].isEnabled() and buttons["删除选中"].isEnabled()
            buttons["扫描选中"].click()
            assert scanned == [(first.id,)]
            buttons["删除选中"].click()
            assert [e.id for e in runtime.static_addresses.entries()] == [second.id]
        finally:
            dialog.reject()

    try:
        QTimer.singleShot(0, interact)
        window.show_static_addresses()
    finally:
        window.closing = True
        window.controller.close()
        window.bridge.worker.shutdown()
        runtime.close()
