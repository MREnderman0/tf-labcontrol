import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.instruments.devices.microchip_53100a.tests.simulation import PhaseSimulation
from lab_control.ui.pages.device_control.phase_noise import PhaseNoisePage, load_results
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
    assert isinstance(page, PhaseNoisePage)
    runtime.monitoring.enable_settings_sync(False)
    try:
        yield page, runtime, key, window
    finally:
        window.controller.close()
        window.closing = True
        window.bridge.worker.shutdown()
        runtime.close()


def test_fixed_capture_is_management_only_and_navigation_never_writes(workspace, qtbot, monkeypatch):
    from PySide6.QtWidgets import QSpinBox

    from lab_control.application.catalog import surface_applicable

    page, runtime, key, window = workspace
    writes = []
    monkeypatch.setattr(runtime.sessions.sessions[key].adapter, "apply", writes.append)
    runtime.repository.save_workspace(
        f"phase-acquisition/{window.controller.mode}/{key}", {"sample_rate": 1, "duration": 999}
    )
    assert window.current_module == "设备管理"
    assert "phase_noise_analyzer" in window._submenu_categories("设备管理")
    assert "phase_noise_analyzer" not in window._submenu_categories("实时监测")
    assert not surface_applicable(window.controller.records[key], "monitoring")
    window.dashboard.refresh()
    assert key not in window.dashboard.cards
    assert not page.findChildren(QSpinBox) and not hasattr(page, "editors")
    window.navigate("实时监测", "phase_noise_analyzer", key)
    assert window.current_module == "设备管理" and window.stack.currentWidget() is page
    qtbot.wait(50)
    assert not writes and not runtime.sessions.sessions[key].adapter.started
    assert not page.stop_button.isEnabled()


def test_missing_signal_shows_reason_and_allows_explicit_retry(workspace, qtbot, monkeypatch):
    from lab_control.domain.instruments import DeviceError

    page, runtime, key, _ = workspace
    adapter = runtime.sessions.sessions[key].adapter
    original = adapter.run_acquisition
    requests = []

    def missing_signal(request, stop):
        requests.append(request)
        raise DeviceError(
            "未检测到 DUT 或参考输入信号；请检查接线与信号源，接好后重新采集。", "signal_missing"
        )
        yield  # Return a generator like the real model's finite acquisition.

    monkeypatch.setattr(adapter, "run_acquisition", missing_signal)
    qtbot.waitUntil(page.start_button.isEnabled)
    page.start_button.click()
    qtbot.waitUntil(lambda: "未检测到" in page.status.text())
    qtbot.waitUntil(page.start_button.isEnabled)
    assert len(requests) == 1 and "自动重连" not in page.status.text()
    assert page.progress.value() == 0 and not page.stop_button.isEnabled()
    assert runtime.sessions.sessions[key].state == "connected"
    assert runtime.sessions.sessions[key].direct_recording.committed == 0
    assert key not in page.results
    monkeypatch.setattr(adapter, "run_acquisition", original)
    page.start_button.click()
    qtbot.waitUntil(lambda: key in page.results, timeout=15000)
    assert page.progress.value() == 1000


def test_fixed_capture_saves_full_phase_and_statistics_then_displays_hidden_page(workspace, qtbot):
    import json
    from pathlib import Path

    import numpy as np

    from lab_control.storage.recording import read_frames

    page, runtime, key, window = workspace
    requests = []
    adapter = runtime.sessions.sessions[key].adapter
    original = adapter.run_acquisition

    def capture(request, stop):
        requests.append(request)
        yield from original(request, stop)

    adapter.run_acquisition = capture
    qtbot.waitUntil(page.start_button.isEnabled)
    page.start_button.click()
    window.navigate("设备管理", "")
    qtbot.waitUntil(lambda: key in page.results, timeout=15000)
    assert requests[0].sample_rate == 1000 and requests[0].duration == 60
    folder = Path(page.paths[key])
    assert folder.parent.parent.name == "单设备采集"
    frames = [
        frame for path in sorted((folder / "_raw").glob("segment-*.sqlite3")) for frame in read_frames(path)
    ]
    phase = [m for f in frames for m in f["measurements"] if m["quantity"] == "phase_difference"]
    saved_x = np.concatenate([m["axis"] for m in phase])
    assert saved_x.size == 60001 and saved_x[-1] - saved_x[0] == pytest.approx(60)
    quantities = {m["quantity"] for f in frames for m in f["measurements"]}
    assert {"phase_difference", "phase_noise", "adev", "mdev"} <= quantities
    assert frames[-1]["configuration"]["complete"]
    window.navigate("设备管理", "phase_noise_analyzer", key)
    assert page.progress.value() == 1000
    np.testing.assert_array_equal(page.curves["phase_difference"].xData, saved_x)
    assert all(page.curves[q].xData is not None for q in ("adev", "mdev", "phase_noise"))
    assert page.start_button.isEnabled() and page.folder.isEnabled()
    recorder = runtime.sessions.sessions[key].direct_recording
    qtbot.waitUntil(recorder.finished.done, timeout=15000)
    assert recorder.finished.exception() is None
    metadata = json.loads((folder / "_raw" / "recording.json").read_text(encoding="utf-8"))
    assert metadata["scope"] == "single_device"
    csv = list(folder.glob("*.csv"))
    assert csv
    headers = "\n".join(p.read_text(encoding="utf-8-sig").splitlines()[0] for p in csv)
    assert "ADEV_平均时间(s)" in headers and "MDEV_平均时间(s)" in headers


def test_early_stop_and_stale_callbacks_preserve_partial_result(workspace, qtbot):
    page, runtime, key, _ = workspace
    page.start()
    qtbot.waitUntil(lambda: page.controller.recorded_frame(key) is not None)
    page.stop()
    qtbot.waitUntil(lambda: key in page.results, timeout=15000)
    assert page.outcomes[key].startswith("已提前停止")
    assert len(page.results[key]["phase_difference"][0]) < 60001
    previous = page.outcomes[key]
    result = page.results[key]
    page.failed(key, RuntimeError("old conversion error"), token=-1)
    page.results_loaded(key, {}, token=-1)
    assert page.outcomes[key] == previous and page.results[key] is result


def test_saved_phase_gaps_are_not_drawn_as_continuous(tmp_path):
    import numpy as np

    from lab_control.domain.instruments import DeviceIdentity, Measurement, MeasurementBatch
    from lab_control.storage.recording import SegmentWriter

    writer = SegmentWriter(tmp_path / "_raw")
    for sequence, epoch in enumerate((0, 0, 1), 1):
        writer.append(
            sequence,
            MeasurementBatch(
                DeviceIdentity("Microchip", "53100A", "TEST"),
                (
                    Measurement(
                        "phase_difference",
                        "s",
                        np.array([1.0, 2.0]),
                        axis=np.array([0.0, 0.001]),
                        axis_unit="s",
                    ),
                ),
                {"acquisition_epoch": epoch},
                validation="full",
            ),
        )
    writer.close()
    x, y = load_results(tmp_path / "_raw")["phase_difference"]
    assert len(x) == 7 and np.isnan(x[4]) and np.isnan(y[4])


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
