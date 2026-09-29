"""Real Qt page, isolated simulation runtime; never opens VISA or a physical device."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QWidget  # noqa: E402

from lab_control.application.config import ConnectionConfig  # noqa: E402
from lab_control.application.runtime import ApplicationRuntime  # noqa: E402
from lab_control.domain.instruments import DeviceCommand, ParameterReading  # noqa: E402
from lab_control.ui.controller import WorkspaceController  # noqa: E402
from lab_control.ui.pages.monitoring.page import MonitoringPage  # noqa: E402


def test_dsa_page_controls_drafts_passive_monitor_manual_save_and_disconnect(qtbot, tmp_path):
    config = ConnectionConfig("mock://dsa-ui", "rigol.dsa815", "mock")
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
        assert page.points.value() == 601 and not page.points.isEnabled()
        editor = page.form.editors["center_frequency"]
        editor.editors["value"].set_value(100e6)
        editor.save_draft()
        editor.show_reading(ParameterReading("center_frequency", 1, 110e6, "instrument"))
        assert editor.dirty and editor.parameters() == {"value": 100e6}
        calls = []
        adapter = app.sessions.sessions[key].adapter
        original = adapter.apply

        def apply(cmd):
            calls.append(cmd.operation)
            return original(cmd)

        adapter.apply = apply
        editor.apply()
        qtbot.waitUntil(lambda: not editor.busy)
        assert calls == ["center_frequency"]
        for op, value in (
            ("rbw_auto", False),
            ("rbw", 10000),
            ("vbw", 3000),
            ("reference_level", -20),
            ("attenuation", 20),
            ("sweep_time_auto", False),
            ("sweep_time", 0.1),
            ("detector", "NORM"),
            ("trace_mode", "WRIT"),
        ):
            control = page.form.editors[op]
            control.editors["value"].set_value(value)
            control.save_draft()
            control.apply()
            qtbot.waitUntil(lambda: not control.busy)
            assert calls[-1] == op
        page.initiate.setChecked(False)
        page.start(True)
        qtbot.waitUntil(lambda: app.monitoring.latest(key, "spectrum") is not None)
        qtbot.waitUntil(lambda: not app.monitoring.tasks[key].running)
        assert not (app.repository.root / "data").exists()
        page.interval.setValue(0.05)
        page.start()
        qtbot.waitUntil(lambda: app.monitoring.tasks[key].sequence >= 2)
        assert not (app.repository.root / "data").exists()
        page.save()
        qtbot.waitUntil(
            lambda: bool(list(app.repository.data_root.glob("单设备采集/*/*_single_frame_*/_raw/samples.npz")))
        )
        app.monitoring.stop(key).result(3)
        editor.editors["value"].set_value(120e6)
        editor.save_draft()
        app.sessions.disconnect(key)
        controller.poll()
        page.refresh()
        assert not page.buttons["once"].isEnabled()
        assert editor.dirty and editor.parameters() == {"value": 120e6}
        controller.close()


def test_dsa_zero_span_and_non_dbm_schema_do_not_mislabel_units():
    from .test_protocol import adapter

    instrument = adapter()
    instrument.capability_details.update(span=0, amplitude_unit="V")
    caps = instrument.capabilities()
    assert caps.get("marker_frequency").parameters["value"].unit == "s"
    assert not caps.get("marker_frequency").writable
    assert caps.get("reference_level").parameters["value"].unit == "V"
    assert not caps.get("reference_level").writable
    try:
        instrument.apply(DeviceCommand("marker_frequency", {"value": 1}))
    except Exception:
        pass
    else:
        raise AssertionError("Frequency write in zero span accepted")
    assert not instrument.transport.history
