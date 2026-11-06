import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLineEdit, QPushButton

from lab_control.application.runtime import ApplicationRuntime
from lab_control.ui.real_window import RealWindow
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox


@pytest.mark.parametrize("adapter,port", [("ceyear.6362d", 10001), ("siglent.sps5000x", 5025)])
def test_address_toolbar_opens_shared_dialog_and_persists_entry(qtbot, tmp_path, adapter, port):
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.timer.stop()
    window.controller.timer.stop()
    seen = []

    def interact():
        dialog = QApplication.activeModalWidget()
        assert dialog is not None
        model = dialog.findChild(WorkspaceComboBox)
        index = model.findData(adapter)
        assert index >= 0
        model.setCurrentIndex(index)
        address = dialog.findChild(QLineEdit, "staticAddressInput")
        address.setText("192.168.0.120")
        save = next(button for button in dialog.findChildren(QPushButton) if button.text() == "保存并扫描")
        qtbot.mouseClick(save, Qt.MouseButton.LeftButton)
        (entry,) = runtime.static_addresses.entries()
        assert (entry.address, entry.adapter, entry.port) == ("192.168.0.120", adapter, port)
        seen.append("saved")
        close = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Close)
        qtbot.mouseClick(close, Qt.MouseButton.LeftButton)

    try:
        assert window.address_list_button.text() == "地址列表"
        assert all(button.text() != "添加资源" for button in window.findChildren(QPushButton))
        QTimer.singleShot(0, interact)
        window.show_static_addresses()
        assert seen == ["saved"]
    finally:
        window.closing = True
        window.controller.close()
        window.bridge.worker.shutdown()
        runtime.close()
