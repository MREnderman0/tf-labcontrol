from PySide6.QtWidgets import QMessageBox, QWidget

from lab_control.domain.instruments import ParameterReading
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.pages.device_control.form import OperationEditor

from .test_adapter import runtime_fixture


def test_ui_channel_ranges_draft_clamping_readonly_and_output_confirmation(qtbot, tmp_path, monkeypatch):
    runtime, key, wire = runtime_fixture(tmp_path)
    parent = QWidget()
    qtbot.addWidget(parent)
    controller = WorkspaceController(runtime, parent)
    try:
        caps = controller.capabilities(key, 3)
        current = OperationEditor(controller, key, caps.get("current"), 3)
        output = OperationEditor(controller, key, caps.get("all_outputs"), 3)
        slave = OperationEditor(controller, key, controller.capabilities(key, 2).get("voltage"), 2)
        for widget in (current, output, slave):
            qtbot.addWidget(widget)
            widget.show()
        assert output.channel == 1
        editor = current.editors["value"]
        editor.input.setText("2")
        current.save_draft()
        wire.voltage[3], wire.current[3], wire.tracking = 6.0, 1.0, "2"
        runtime.sessions.submit(key, "refresh_capabilities").result()
        controller.poll()
        assert editor.spec.maximum == 1
        editor.finish_editing(force=True)
        assert editor.value() == 1
        assert not slave.apply_button.isVisible() and not slave.editors["value"].isEnabled()
        assert "CH1" in slave.range_note.text()
        current.show_reading(ParameterReading("current", 3, 1.2, "instrument"))
        assert "1.2" in current.readback.text()  # Never clamp actual readback to the input maximum.
        output.editors["value"].input.setChecked(True)
        monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.No)
        output.apply()
        assert not wire.output
        assert all(payload.endswith("?") or payload == "REMOTE" for payload in wire.history)
    finally:
        controller.close()
        runtime.close()
