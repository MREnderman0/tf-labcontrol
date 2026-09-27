"""FPH registry contract on real Qt widgets; isolated simulation, no VISA."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QWidget

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import ParameterReading
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.pages.monitoring.page import MonitoringPage


def test_fph_controls_monitor_save_draft_and_disconnect(qtbot, tmp_path):
    config = ConnectionConfig("mock://fph-ui", "rs.fph", "mock")
    with ApplicationRuntime((config,), root=tmp_path, hardware=False) as app:
        key = app.add(config)
        app.connect(key).result()
        parent = QWidget()
        qtbot.addWidget(parent)
        controller = WorkspaceController(app, parent)
        page = MonitoringPage(controller, "spectrum_analyzer", lambda *args: None)
        qtbot.addWidget(page)
        page.resize(1280, 760)
        page.show()
        page.select_device(key)
        assert page.points.value() == 711 and not page.points.isEnabled()
        adapter = app.sessions.sessions[key].adapter
        calls = []
        apply = adapter.apply
        adapter.apply = lambda command: calls.append(command.operation) or apply(command)
        for op, value in [
            ("center_frequency", 100e6),
            ("rbw_auto", False),
            ("rbw", 10000),
            ("vbw", 3000),
            ("reference_level", -20),
            ("attenuation", 20),
            ("sweep_time_auto", False),
            ("sweep_time", 0.1),
            ("detector", "SAMP"),
            ("trace_mode", "WRIT"),
        ]:
            editor = page.form.editors[op]
            editor.editors["value"].set_value(value)
            editor.save_draft()
            editor.apply()
            qtbot.waitUntil(lambda: not editor.busy)
            assert calls[-1] == op
        editor = page.form.editors["center_frequency"]
        editor.editors["value"].set_value(110e6)
        editor.save_draft()
        editor.show_reading(ParameterReading("center_frequency", 1, 100e6, "instrument"))
        page.initiate.setChecked(False)
        page.start()
        qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
        assert editor.dirty and editor.parameters() == {"value": 110e6}
        assert not (app.repository.root / "data").exists()
        app.monitoring.stop(key).result(3)
        page.save()
        qtbot.waitUntil(lambda: bool(list(app.repository.data_root.rglob("samples.npz"))))
        app.sessions.disconnect(key)
        controller.poll()
        page.refresh()
        assert not page.buttons["once"].isEnabled() and editor.dirty
        controller.close()
