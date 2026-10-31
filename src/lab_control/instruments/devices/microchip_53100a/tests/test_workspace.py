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


def test_acquisition_preferences_survive_restarts_and_device_switches(qtbot, tmp_path, monkeypatch):
    expected = {}
    for launch in range(2):
        with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
            runtime.sessions.factory = PhaseSimulation
            keys = []
            writes = []
            for index in range(2):
                key = runtime.add(
                    ConnectionConfig(
                        f"mock://phase-preferences/{index}",
                        "microchip.53100a",
                        "mock",
                        usb_instance_id=f"TEST-PREFERENCES-{index}",
                    )
                )
                runtime.connect(key).result(3)
                monkeypatch.setattr(runtime.sessions.sessions[key].adapter, "apply", writes.append)
                keys.append(key)
            window = RealWindow(runtime)
            qtbot.addWidget(window)
            window.controller.timer.stop()
            runtime.monitoring.enable_settings_sync(False)
            try:
                for index, key in enumerate(keys):
                    window.navigate("实时监测", "phase_noise_analyzer", key)
                    page = window.stack.currentWidget()
                    assert (page.rate.currentData(), page.duration.value()) == expected.get(key, (100, 60))
                    if launch == 0:
                        rate, duration = (10, 20) if index == 0 else (1000, 40)
                        page.rate.setCurrentIndex(page.rate.findData(rate))
                        page.duration.setValue(duration)
                        expected[key] = rate, duration
                    assert runtime.repository.load_workspace(page.acquisition_key) == dict(
                        sample_rate=expected[key][0], duration=expected[key][1]
                    )
                page.select_device(keys[0])
                assert (page.rate.currentData(), page.duration.value()) == expected[keys[0]]
                assert not writes and not runtime.monitoring.tasks
                assert all(not session.adapter.started for session in runtime.sessions.sessions.values())
            finally:
                window.controller.close()
                window.closing = True
                window.bridge.worker.shutdown()


@pytest.mark.parametrize(
    "saved", [None, {"sample_rate": 123, "duration": 1}, {"sample_rate": [], "duration": "bad"}]
)
def test_invalid_acquisition_preferences_use_supported_defaults(workspace, saved):
    page, runtime, key, _ = workspace
    pref = page.acquisition_key
    runtime.repository.save_workspace(pref, saved)
    page.select_device("")
    page.select_device(key)
    assert (page.rate.currentData(), page.duration.value()) == (100, 60)
    assert runtime.repository.load_workspace(pref) == saved
    assert not runtime.sessions.sessions[key].adapter.started


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


def test_phase_start_stays_disabled_during_binary_drain_but_not_csv(workspace, qtbot, monkeypatch):
    import threading

    from lab_control.storage.recording_workers import CsvConversionSupervisor

    page, runtime, key, window = workspace
    entered, release, release_csv = threading.Event(), threading.Event(), threading.Event()
    factory = runtime.repository.create_recording
    convert = CsvConversionSupervisor.run

    def hold_csv(supervisor):
        supervisor.recording.thread.join()
        assert release_csv.wait(15)
        convert(supervisor)

    def create(*args, **kwargs):
        recorder = factory(*args, **kwargs)
        append = recorder.segments.append

        def write(sequence, batch):
            entered.set()
            assert release.wait(15)
            return append(sequence, batch)

        recorder.segments.append = write
        return recorder

    monkeypatch.setattr(runtime.repository, "create_recording", create)
    monkeypatch.setattr(CsvConversionSupervisor, "run", hold_csv)
    try:
        qtbot.waitUntil(page.start_button.isEnabled)
        page.start_button.click()
        qtbot.waitUntil(entered.is_set)
        window.controller.poll()
        page.stop_button.click()
        window.controller.poll()
        recorder = runtime.sessions.sessions[key].direct_recording
        assert not recorder.binary_done and not page.start_button.isEnabled()
        page.start_button.click()
        assert runtime.sessions.sessions[key].direct_recording is recorder
        release.set()
        qtbot.waitUntil(lambda: recorder.binary_done)
        qtbot.waitUntil(page.start_button.isEnabled)
        assert not recorder.finished.done()
    finally:
        release.set()
        release_csv.set()
