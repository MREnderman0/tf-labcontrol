from dataclasses import replace

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QWidget

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest
from lab_control.instruments.devices.lecroy_waverunner9404m.tests.test_sessions import Scope
from lab_control.instruments.devices.rigol_dho924.tests.fake import Peer
from lab_control.instruments.registry import build_scpi_adapter
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.pages.monitoring.oscilloscope_controls import OscilloscopeControls


@pytest.fixture(params=["lecroy.9404m", "rigol.dho924"])
def panel(request, qtbot, tmp_path):
    config = ConnectionConfig("mock://rate-ui", request.param, "mock")
    peer = Scope() if request.param == "lecroy.9404m" else Peer()
    with ApplicationRuntime(root=tmp_path, hardware=False) as app:
        app.sessions.factory = lambda selected: build_scpi_adapter(selected, peer)
        key = app.add(config)
        app.connect(key).result(5)
        host = QWidget()
        qtbot.addWidget(host)
        controller = WorkspaceController(app, host)
        app.monitoring.enable_settings_sync(False)
        controller.timer.stop()
        controls = OscilloscopeControls(controller, key, lambda: None, host)
        controls.resize(360, 700)
        host.resize(360, 700)
        host.show()
        try:
            yield controls, app, key, peer
        finally:
            controller.close()


def publish(panel, rate, *, stale=False):
    controls, app, key, _ = panel
    session = app.sessions.sessions[key]
    snapshot = session.capability_snapshot
    session.capability_snapshot = replace(
        snapshot, details={**snapshot.details, "memory": {"sample_rate": rate}}, stale=stale
    )
    controls.controller.poll()


def test_real_model_snapshots_display_without_frame_or_io(panel):
    controls, app, key, peer = panel
    snapshot = app.sessions.capability_snapshot(key)
    assert snapshot.details["memory"]["sample_rate"] > 0
    assert "Sa/s" in controls.sample_rate.text()
    assert "缓存" in controls.sample_rate.toolTip()
    before = peer.history.copy()
    for _ in range(20):
        controls.refresh()
    assert peer.history == before
    # Stable frames keep their original query contract; UI refresh adds none.
    req = AcquisitionRequest("waveform", (1,), consistency="live", context_id="rate-ui")
    app.sessions.submit(key, "acquire", req).result(5)
    before = len(peer.history)
    app.sessions.submit(key, "acquire", req).result(5)
    queries = [q for q in peer.history[before:] if "?" in q]
    assert queries == (
        ["C1:WF? ALL"] if isinstance(peer, Scope) else [":WAV:PRE?", ":WAV:DATA?", ":WAV:PRE?"]
    )
    before = peer.history.copy()
    controls.controller.poll()
    assert peer.history == before


@pytest.mark.parametrize(
    "rate,text",
    [
        (250, "250 Sa/s"),
        (1250, "1.25 kSa/s"),
        (500e6, "500 MSa/s"),
        (2.5e9, "2.5 GSa/s"),
        (None, "—"),
        (0, "—"),
        (-1, "—"),
        (float("nan"), "—"),
        (float("inf"), "—"),
        (True, "—"),
        ("500e6", "—"),
    ],
)
def test_units_and_invalid_values(panel, rate, text):
    publish(panel, rate)
    assert panel[0].sample_rate.text() == f"采样率：{text}"


def test_pending_stale_locked_and_reconnected_states(panel):
    controls, app, key, _ = panel
    publish(panel, 500e6)
    controls.running = True
    publish(panel, 1e9)
    assert controls.sample_rate.text() == "采样率：500 MSa/s"
    controls.running = False
    controls.refresh()
    assert controls.sample_rate.text() == "采样率：1 GSa/s"
    publish(panel, 2e9, stale=True)
    assert controls.sample_rate.text() == "采样率：1 GSa/s · 待更新"
    assert controls.sample_rate.property("stale")
    publish(panel, 250e6)
    session = app.sessions.sessions[key]
    session.recording = True
    controls.controller.poll()
    assert controls.sample_rate.text() == "采样率：250 MSa/s" and controls.sample_rate.isEnabled()
    session.recording = False
    app.sessions.disconnect(key)
    controls.controller.poll()
    assert controls.sample_rate.text() == "采样率：—"
    app.connect(key).result(5)
    controls.controller.poll()
    assert "Sa/s" in controls.sample_rate.text() and "250 M" not in controls.sample_rate.text()


def test_card_order_rate_position_and_fixed_footer(panel):
    controls, _, _, _ = panel
    stack = controls.scroller.widget().layout()
    cards = [stack.itemAt(i).widget() for i in range(stack.count() - 1)]
    assert len(cards) == 6  # Horizontal, four channels, trigger; no sampling card.
    assert controls.sample_rate.parentWidget() is cards[0]
    assert [controls.checks[ch].parentWidget() for ch in range(1, 5)] == cards[1:5]
    assert controls.trigger_label.parentWidget() is cards[-1]
    assert controls.trigger_slider.parentWidget() is cards[-1] and controls.gear.parentWidget() is cards[-1]
    assert controls.sample_rate.y() >= controls.points.y() + controls.points.height()
    footer = controls.fine_button.mapTo(controls, QPoint())
    controls.scroller.verticalScrollBar().setValue(controls.scroller.verticalScrollBar().maximum())
    assert controls.fine_button.mapTo(controls, QPoint()) == footer


def test_explicit_parameter_refresh_publishes_rate_once(panel, monkeypatch):
    controls, app, key, peer = panel
    original = peer.query

    def query(command):
        if command == ":ACQ:SRAT?" or command == "VBS? 'return=app.Acquisition.Horizontal.SamplingRate'":
            peer.history.append(command)
            return b"500000000"
        return original(command)

    monkeypatch.setattr(peer, "query", query)
    app.sessions.submit(key, "refresh_capabilities").result(5)
    before = peer.history.copy()
    controls.controller.poll()
    assert controls.sample_rate.text() == "采样率：500 MSa/s" and peer.history == before


def test_switching_model_uses_its_own_snapshot(panel, qtbot):
    controls, app, _, peer = panel
    publish(panel, 500e6)
    other = Peer() if isinstance(peer, Scope) else Scope()
    model = "rigol.dho924" if isinstance(other, Peer) else "lecroy.9404m"
    app.sessions.factory = lambda config: build_scpi_adapter(config, other)
    key = app.add(ConnectionConfig("mock://other-rate", model, "mock"))
    app.connect(key).result(5)
    controls.controller.poll()
    new_controls = OscilloscopeControls(controls.controller, key, lambda: None)
    qtbot.addWidget(new_controls)
    assert new_controls.sample_rate.text() == "采样率：1 GSa/s"
    assert controls.sample_rate.text() == "采样率：500 MSa/s"
