"""Offline Qt review of the read-only sampling rate and final trigger card."""

import argparse
import json
import os
import time
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory


def render(output: Path, scale: str) -> None:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_SCALE_FACTOR"] = scale
    import numpy as np
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    from lab_control.app import load_stylesheet, load_ui_font
    from lab_control.application.config import ConnectionConfig
    from lab_control.application.runtime import ApplicationRuntime
    from lab_control.instruments.devices.rigol_dho924.tests.fake import Peer
    from lab_control.instruments.registry import build_scpi_adapter
    from lab_control.transport.base import ieee_block
    from lab_control.ui.real_window import RealWindow

    app = QApplication([])
    app.setStyle("Fusion")
    app.setFont(QFont(load_ui_font(), 10))
    app.setStyleSheet(load_stylesheet())
    output.mkdir(parents=True, exist_ok=True)

    def pump(seconds=0.1):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.002)

    def wait(predicate):
        deadline = time.monotonic() + 10
        while not predicate() and time.monotonic() < deadline:
            pump(0.01)
        assert predicate()

    evidence = []
    with TemporaryDirectory(prefix="scope-rate-review-") as folder:
        runtime = ApplicationRuntime(root=Path(folder), hardware=False)
        peer = Peer()
        peer.state[":ACQ:SRAT"] = 500e6
        original = peer.query

        def query(command):
            raw = original(command)
            if command == ":WAV:DATA?":
                ch = int(peer.state[":WAV:SOUR"][-1])
                codes = 32768 + 3000 * np.sin(np.linspace(0, 12, 1000) + ch) + (2.4 - (ch - 1) * 1.6) * 7500
                return ieee_block(codes.astype("<u2").tobytes()) + b"\n"
            return raw

        peer.query = query
        runtime.sessions.factory = lambda config: build_scpi_adapter(config, peer)
        key = runtime.add(ConnectionConfig("mock://rate-review", "rigol.dho924", "mock"))
        runtime.connect(key).result(5)
        window = RealWindow(runtime)
        window.show()
        window.navigate("实时监测", "oscilloscope", key)
        page = window.stack.currentWidget()
        try:
            wait(lambda: page.controls and len(page.controls.values) >= 27 and not page.controls.running)
            page.start()
            wait(lambda: page.plot.displayed_frame is not None)
            runtime.monitoring.stop(key).result(5)
            runtime.monitoring.enable_settings_sync(False)
            page.controller.poll()
            controls = page.controls
            for width, height in ((1280, 760), (1440, 900)):
                window.resize(width, height)
                for view in ("rate", "trigger", "stale", "locked"):
                    session = runtime.sessions.sessions[key]
                    session.capability_snapshot = replace(session.capability_snapshot, stale=view == "stale")
                    session.recording = view == "locked"
                    page.controller.poll()
                    bar = controls.scroller.verticalScrollBar()
                    bar.setValue(bar.maximum() if view == "trigger" else 0)
                    pump()
                    for widget in (controls.auto_button, controls.fine_button):
                        pos = widget.mapTo(window, QPoint())
                        assert 0 <= pos.y() <= height - widget.height()
                    shown = controls.trigger_slider if view == "trigger" else controls.sample_rate
                    rect = shown.rect().translated(shown.mapTo(controls.scroller.viewport(), QPoint()))
                    assert controls.scroller.viewport().rect().contains(rect)
                    assert "500 MSa/s" in controls.sample_rate.text()
                    if view != "trigger":
                        assert (
                            controls.sample_rate.fontMetrics().horizontalAdvance(controls.sample_rate.text())
                            <= controls.sample_rate.width()
                        )
                    name = f"scope-{view}-{width}x{height}-{scale}.png"
                    assert window.grab().save(str(output / name))
                    evidence.append(
                        {"file": name, "dpr": window.devicePixelRatioF(), "rate": controls.sample_rate.text()}
                    )
            runtime.sessions.sessions[key].recording = False
        finally:
            window.timer.stop()
            page.controller.close()
            runtime.close()
            window.closing = True
            window.close()
            window.bridge.worker.shutdown()
    (output / f"render-{scale}.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("build/scope-rate-review"))
    parser.add_argument("--scale", default="1")
    args = parser.parse_args()
    render(args.output, args.scale)
