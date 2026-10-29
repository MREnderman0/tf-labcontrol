"""Catch transient native windows even when they disappear before a screenshot."""

import pytest
import shiboken6
from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QWidget

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.ui.pages.monitoring.optical_power import PowerMeterReadoutCard
from lab_control.ui.pages.monitoring.optical_spectrum_controls import OpticalSpectrumControls
from lab_control.ui.real_window import RealWindow


class WindowShows(QObject):
    def __init__(self):
        super().__init__()
        self.allowed = set()
        self.unexpected = []

    def eventFilter(self, obj, event):  # noqa: N802
        if (
            event.type() == QEvent.Type.Show
            and isinstance(obj, QWidget)
            and obj.isWindow()
            and obj not in self.allowed
        ):
            self.unexpected.append((type(obj).__name__, obj.objectName(), obj.windowTitle()))
        return False


@pytest.fixture
def workspace(qapp, qtbot, tmp_path):
    events = WindowShows()
    qapp.installEventFilter(events)
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    events.allowed.add(window)
    window.show()
    window.timer.stop()
    window.controller.timer.stop()
    try:
        yield window, runtime, events
    finally:
        qapp.removeEventFilter(events)
        window.controller.close()
        window.closing = True
        window.bridge.worker.shutdown()
        window.storage_panel.shutdown()
        window.environment_panel.shutdown()
        runtime.close()
        window.hide()


def add_pair(runtime, adapter):
    devices = []
    for index in range(2):
        key = runtime.add(ConnectionConfig(f"mock://window-test/{adapter}/{index}", adapter, "mock"))
        runtime.connect(key).result(5)
        devices.append(key)
    return devices


@pytest.mark.parametrize(
    "module,category,adapter",
    [
        ("实时监测", "optical_power_meter", "joinwit.jw8105"),
        ("实时监测", "optical_spectrometer", "ceyear.6362d"),
        ("实时监测", "oscilloscope", "lecroy.9404m"),
        ("实时监测", "spectrum_analyzer", "rigol.dsa815"),
        ("实时监测", "power_supply", "gwinstek.gpd4303s"),
        ("实时监测", "signal_generator", "rs.sma100b"),
        ("实时监测", "frequency_counter", "kplusk.fxe"),
        ("实时监测", "phase_noise_analyzer", "microchip.53100a"),
        ("实时监测", "multimeter", "keysight.3458a"),
        ("实时监测", "network_analyzer", "rs.znb3004"),
        ("设备管理", "signal_generator", "rs.sma100b"),
        ("设备管理", "power_supply", "coremorrow.xmt"),
    ],
)
def test_refresh_switch_and_reconnect_never_show_embedded_controls_as_windows(
    workspace, qtbot, module, category, adapter
):
    window, runtime, events = workspace
    first, second = add_pair(runtime, adapter)
    window.controller.poll()
    window.refresh()
    window.navigate(module, category, first)
    qtbot.wait(30)
    page = window.stack.currentWidget()
    selector = page.selector

    def ready():
        window.controller.poll()
        return selector.isEnabled()

    qtbot.waitUntil(ready)
    combo = selector.combo
    popup = combo.view().window()
    events.allowed.add(popup)
    # Exercise the actual dropdown commit, not just direct page rebinding.
    combo.showPopup()
    qtbot.waitUntil(popup.isVisible)
    view = combo.view()
    index = combo.model().index(combo.findData(second), 0)
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualRect(index).center())
    qtbot.waitUntil(lambda: selector.device_id == second)
    assert not popup.isVisible()
    events.allowed.remove(popup)
    for device_id in (first, second, first):
        qtbot.waitUntil(ready)
        window.navigate(module, category, device_id)
        window.controller.poll()
        qtbot.wait(30)
    for width in (1000, 1500):
        window.resize(width, 900)
        window.controller.poll()
        qtbot.wait(30)
    window.navigate("设备管理")

    def second_idle():
        window.controller.poll()
        state = window.controller.states[second]
        return not (state["pending"] or state["controlPending"] or state["controlGroup"])

    # Navigation can still be reading parameters after the fixed paint wait.
    # Reconnect only after that work finishes; preserve normal background draining.
    qtbot.waitUntil(second_idle)
    runtime.sessions.disconnect(second, drain_reads=True)
    window.controller.poll()
    runtime.connect(second).result(5)
    window.controller.poll()
    window.refresh()
    window.navigate(module, category, second)
    qtbot.wait(30)
    assert not events.unexpected
    assert not runtime.monitoring.tasks


def test_hidden_power_channels_stay_owned_and_are_retired_on_device_switch(workspace, qtbot, qapp):
    window, runtime, events = workspace
    first, second = add_pair(runtime, "joinwit.jw8105")
    runtime.repository.save_workspace("monitoring_channels/optical_power_meter", {f"device:{first}": [1, 2]})
    window.controller.poll()
    window.navigate("实时监测", "optical_power_meter", first)
    page = window.stack.currentWidget()
    assert page.channels() == (1, 2)
    # Include channels not currently in the grid when checking lifetime.
    page.channel_checks[16].setChecked(False)
    old_cards = list(page.cards.values())
    assert all(card.parentWidget() is page.readout_body for card in old_cards)
    assert not page.cards[16].isVisible()
    page.selector.select(second)
    assert all(not card.isVisible() for card in old_cards)
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert all(not shiboken6.isValid(card) for card in old_cards)
    assert len(page.readout_body.findChildren(PowerMeterReadoutCard)) == 16
    for check in page.channel_checks.values():
        check.setChecked(False)
    assert not any(card.isVisible() for card in page.cards.values())
    page.channel_checks[1].setChecked(True)
    qtbot.wait(30)
    assert page.cards[1].isVisible()
    assert not events.unexpected


def test_explicit_monitor_details_still_opens_only_on_request(workspace, qtbot):
    window, runtime, events = workspace
    first, _ = add_pair(runtime, "joinwit.jw8105")
    window.controller.poll()
    window.navigate("实时监测")
    card = window.dashboard.cards[first]
    assert not card.details_dialog.isVisible() and not events.unexpected
    events.allowed.add(card.details_dialog)
    card.show_details()
    qtbot.waitUntil(card.details_dialog.isVisible)
    card.details_dialog.close()
    assert not events.unexpected


def test_rapid_optical_spectrum_switch_skips_queued_reads_for_retired_editors(workspace, qtbot, monkeypatch):
    window, runtime, events = workspace
    first, second = add_pair(runtime, "ceyear.6362d")
    started = []
    refresh = OpticalSpectrumControls.refresh_parameters

    def traced_refresh(controls):
        started.append((controls.device_id, controls))
        refresh(controls)

    monkeypatch.setattr(OpticalSpectrumControls, "refresh_parameters", traced_refresh)
    window.controller.poll()
    window.navigate("实时监测", "optical_spectrometer", first)
    page = window.stack.currentWidget()
    # Switch again before the event loop delivers the queued initial read.
    page.selector.select(second)
    page.selector.select(first)
    selected = page.controls

    def finished():
        window.controller.poll()
        return bool(started) and not selected.running

    qtbot.waitUntil(finished)
    assert started == [(first, selected)]
    assert not window.controller.group_devices
    assert page.selector.isEnabled()
    assert not events.unexpected
