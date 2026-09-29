import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.instruments.devices.microchip_53100a.tests.simulation import PhaseSimulation
from lab_control.ui.pages.monitoring.phase_noise import PhaseNoiseMonitoringPage
from lab_control.ui.real_window import RealWindow


@pytest.fixture
def workspace(qtbot, tmp_path):
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    runtime.sessions.factory = PhaseSimulation
    key = runtime.add(
        ConnectionConfig("mock://phase-workspace", "microchip.53100a", "mock", usb_instance_id="TEST-USB")
    )
    runtime.connect(key).result(3)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    window.navigate("设备管理", "phase_noise_analyzer", key)
    page = window.stack.currentWidget()
    assert isinstance(page, PhaseNoiseMonitoringPage)
    runtime.monitoring.enable_settings_sync(False)
    try:
        yield page, runtime, key, window
    finally:
        window.controller.close()
        window.closing = True
        window.bridge.worker.shutdown()
        runtime.close()


def test_phase_workspace_has_only_requested_controls_and_no_start_on_navigation(workspace):
    page, runtime, key, _ = workspace
    assert [page.rate.itemData(i) for i in range(page.rate.count())] == [1, 10, 100, 1000]
    assert page.duration.value() == 60
    assert len(page.editors) == 2
    assert not runtime.sessions.sessions[key].adapter.started
    assert not page.stop_button.isEnabled()


def test_auto_frequency_readback_uses_shared_idle_sync_without_starting_measurement(workspace, qtbot):
    page, runtime, key, _ = workspace
    runtime.monitoring.enable_settings_sync(True)
    page.focus_settings()
    qtbot.waitUntil(lambda: all("当前：自动" == c.readback.text() for c in page.editors), timeout=5000)
    assert not runtime.sessions.sessions[key].adapter.started


def test_finite_ui_run_keeps_results_and_enables_controls_after_binary_completion(workspace, qtbot):
    page, runtime, key, window = workspace
    page.duration.setValue(10)
    qtbot.waitUntil(lambda: page.start_button.isEnabled())
    page.start()
    qtbot.waitUntil(lambda: runtime.sessions.sessions[key].adapter.started)
    window.controller.poll()
    assert not page.rate.isEnabled() and page.stop_button.isEnabled()
    qtbot.waitUntil(lambda: page.outcomes.get(key, "").startswith("采集完成"), timeout=15000)
    assert page.progress.value() == 1000
    assert all(page.curves[q].xData is not None for q in ("phase_noise", "adev", "mdev"))
    assert page.rate.isEnabled() and page.folder.isEnabled()
    window.navigate("实时监测", "")
    window.navigate("实时监测", "phase_noise_analyzer", key)
    assert page.curves["mdev"].xData is not None


def test_manual_frequency_requires_application_before_start_and_auto_omits_value(workspace, qtbot):
    page, runtime, key, _ = workspace
    control = page.editors[0]
    control.auto.setChecked(False)
    page.refresh()
    assert control.value.isEnabled() and not page.start_button.isEnabled()
    control.apply()
    qtbot.waitUntil(lambda: not control.busy)
    assert runtime.sessions.sessions[key].adapter.values["1.input_frequency"]["value"] == 10e6
    control.auto.setChecked(True)
    control.apply()
    qtbot.waitUntil(lambda: not control.busy)
    assert runtime.sessions.sessions[key].adapter.values["1.input_frequency"] == {"automatic": True}


def test_early_stop_is_partial_and_old_csv_callback_cannot_replace_new_status(workspace, qtbot):
    page, runtime, key, _ = workspace
    page.start()
    qtbot.waitUntil(lambda: runtime.sessions.sessions[key].adapter.started)
    qtbot.waitUntil(lambda: page.controller.recorded_frame(key) is not None)
    page.stop()
    qtbot.waitUntil(lambda: page.outcomes.get(key, "").startswith("已提前停止"), timeout=15000)
    previous = page.outcomes[key]
    page.failed(key, RuntimeError("old conversion error"), token=-1)
    assert page.outcomes[key] == previous
