"""Exercise rendered geometry while values, report order and window size change."""

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QFont

from lab_control.app import load_stylesheet, load_ui_font
from lab_control.application.controls import DataSource
from lab_control.application.monitoring_state import MonitorFrame
from lab_control.domain.instruments import AcquisitionRequest, DeviceIdentity, Measurement, MeasurementBatch
from lab_control.ui.pages.monitoring.plot import LivePlot


@pytest.fixture
def plot(qapp, qtbot):
    qapp.setStyle("Fusion")
    qapp.setFont(QFont(load_ui_font(), 10))
    qapp.setStyleSheet(load_stylesheet())
    widget = LivePlot()
    qtbot.addWidget(widget)
    widget.resize(1140, 600)
    widget.show()
    return widget


def frame(values, *, sequence, quantity="frequency", unit="Hz"):
    channels = tuple(values)
    return MonitorFrame(
        sequence,
        0,
        AcquisitionRequest("scalar", channels, quantity=quantity),
        MeasurementBatch(
            DeviceIdentity("Mock", "Counter", "layout-test"),
            tuple(
                Measurement(quantity, unit, (value,), channel=channel) for channel, value in values.items()
            ),
            {},
        ),
        session_id="layout-test",
        received_at=float(sequence),
    )


def geometry(plot):
    return {
        key: (
            card.channel.mapTo(plot, QPoint()).toTuple(),
            card.value.mapTo(plot, QPoint()).toTuple(),
            card.value.size().toTuple(),
        )
        for key, card in plot.value.cards.items()
    }


@pytest.mark.parametrize("width", [1140, 760, 400])
@pytest.mark.parametrize("quantity,unit", [("frequency", "Hz"), ("voltage", "V")])
def test_channel_anchors_stay_fixed_as_digits_sign_and_exponent_change(plot, qtbot, width, quantity, unit):
    plot.resize(width, 600)
    source = DataSource(quantity, quantity, "scalar", quantity, quantity)
    patterns = [
        {1: 1.0, 2: 2.0, 3: 3.0, 4: 4.0},
        {4: -0.0000000000123456789, 2: 123456789012.0, 1: -9876543210.12, 3: 9.99999999999e12},
        {1: 9999999.99999, 2: -1.0, 3: 1e-30, 4: 10000000.0001},
    ]
    anchors = None
    for sequence, values in enumerate(patterns, 1):
        plot.show_frame(frame(values, sequence=sequence, quantity=quantity, unit=unit), source, (1, 2, 3, 4))
        qtbot.wait(15)
        current = geometry(plot)
        if anchors is None:
            anchors = current
        assert current == anchors
        for card in plot.value.cards.values():
            assert card.isVisible()
            assert card.value.fontMetrics().horizontalAdvance(card.value.text()) <= card.value.width()
    # A missing channel does not move its neighbours or display its old value.
    plot.show_frame(
        frame({1: 1.0, 3: 3.0, 4: 4.0}, sequence=4, quantity=quantity, unit=unit), source, (1, 2, 3, 4)
    )
    qtbot.wait(15)
    assert geometry(plot) == anchors
    assert plot.value.cards[(2, quantity)].value.text() == "—"
    plot.show_frame(frame({}, sequence=5, quantity=quantity, unit=unit), source, (1, 2, 3, 4))
    qtbot.wait(15)
    assert plot.value.isVisible() and geometry(plot) == anchors
    assert all(card.value.text() == "—" for card in plot.value.cards.values())


def test_many_channels_scroll_without_expanding_page_and_selection_clears_old_readings(plot, qtbot):
    source = DataSource("frequency", "频率", "scalar", "frequency", "frequency")
    values = dict.fromkeys(range(1, 25), 10000000.0001)
    plot.show_frame(frame(values, sequence=1), source, tuple(values))
    qtbot.wait(20)
    assert plot.height() == 600
    assert plot.value.verticalScrollBar().maximum() > 0
    plot.value.verticalScrollBar().setValue(plot.value.verticalScrollBar().maximum())
    last = plot.value.cards[(24, "frequency")]
    assert 0 <= last.mapTo(plot.value.viewport(), QPoint()).y() < plot.value.viewport().height()
    plot.show_frame(frame({2: 2.0, 4: 4.0}, sequence=2), source, (2, 4))
    qtbot.wait(20)
    assert set(plot.value.cards) == {(2, "frequency"), (4, "frequency")}
    assert plot.value.verticalScrollBar().maximum() == 0
    plot.reset()
    assert not plot.value.cards and plot.displayed_frame is None
