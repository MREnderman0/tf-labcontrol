"""Render the M300 monitoring layout offline, using synthetic temperature data."""

from __future__ import annotations

import argparse
import math
import os
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from lab_control.app import load_stylesheet, load_ui_font
from lab_control.application.config import ConnectionConfig
from lab_control.application.monitoring_state import MonitorFrame
from lab_control.application.runtime import ApplicationRuntime
from lab_control.demo.instrument import SimulatedInstrument
from lab_control.domain.instruments import Measurement, MeasurementBatch
from lab_control.ui.real_window import RealWindow


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("tmp/reviews/m300-channel-list"))
    output = parser.parse_args().output
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setFont(load_ui_font())
    app.setStyleSheet(load_stylesheet())
    config = ConnectionConfig("mock://m300-ui-review", "rigol.m300", "mock", expected_serial="SIMULATED")
    runtime = ApplicationRuntime(root=output / "mock-runtime", hardware=False)
    instrument = SimulatedInstrument(config)
    channels = tuple(range(401, 420))
    instrument.caps = replace(instrument.caps, channels=(1, *range(401, 433)))
    instrument.capability_details = {
        "acquisition_channel_selection": "scan_list",
        "acquisition_channels": list(channels),
    }
    runtime.sessions.factory = lambda _config: instrument
    device = runtime.add(config)
    runtime.connect(device).result(5)
    window = RealWindow(runtime)
    try:
        window.navigate("实时监测", "multimeter", device)
        page = window.stack.currentWidget().scan_page
        window.show()
        window.timer.stop()
        window.controller.timer.stop()
        app.processEvents()
        app.processEvents()
        request = page.request(channels, confirmed=True)
        for index in range(181):
            measurements = tuple(
                Measurement("temperature", "°C", (23.0 + i * 0.19 + 0.1 * math.sin(index / 20 + i),), ch)
                for i, ch in enumerate(channels)
            )
            frame = MonitorFrame(
                index,
                0,
                request,
                MeasurementBatch(instrument.identity, measurements, {}),
                received_at=index * 3.0,
                session_id="offline-preview",
            )
            page.plot.show_frame(frame, channels)
        for measurement in measurements:
            page.cards[measurement.channel].update_measurement(measurement)
        page.trend_label.setText("温度趋势（°C）")
        page.status.setText("离线布局预览 · 模拟温度数据")
        page.channel_checks[408].setChecked(False)
        for name, size in (("1280x760", (1280, 760)), ("1440x900", (1440, 900)), ("900x760", (900, 760))):
            window.resize(*size)
            for _ in range(3):
                app.processEvents()
            assert window.width() == size[0] and window.height() == size[1]
            assert page.readout_scroll.horizontalScrollBar().maximum() == 0
            assert page.readout_grid.itemAtPosition(0, 1).widget() is page.cards[402]
            assert page.readout_grid.itemAtPosition(9, 0).widget() is page.cards[419]
            for card in page.cards.values():
                assert card.value.fontMetrics().horizontalAdvance(card.value.text()) <= card.value.width()
            page.status.setText("离线布局预览 · 模拟温度数据")
            window.grab().save(str(output / f"{name}.png"))
            print(f"{name}: actual={window.width()}x{window.height()}, page={page.width()}x{page.height()}")
    finally:
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.closing = True
        window.bridge.worker.shutdown()
        window.storage_panel.shutdown()
        window.environment_panel.shutdown()


if __name__ == "__main__":
    main()
