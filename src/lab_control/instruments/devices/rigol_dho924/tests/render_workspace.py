"""Offline official-theme review using an isolated SCPI peer, never hardware."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory


def render(output: Path, scale: str):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_SCALE_FACTOR"] = scale
    import numpy as np
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    from lab_control.app import load_stylesheet, load_ui_font
    from lab_control.application.config import ConnectionConfig
    from lab_control.application.runtime import ApplicationRuntime
    from lab_control.transport.base import ieee_block
    from lab_control.ui.real_window import RealWindow

    from ..adapter import DHO924Adapter
    from ..profile import profile
    from .fake import Peer

    app = QApplication([])
    app.setStyle("Fusion")
    app.setFont(QFont(load_ui_font(), 10))
    app.setStyleSheet(load_stylesheet())
    output.mkdir(parents=True, exist_ok=True)

    def pump(seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            app.processEvents()
            time.sleep(0.005)

    class Curves(Peer):
        def query(self, payload):
            raw = super().query(payload)
            ch = int(self.state[":WAV:SOUR"][-1])
            n = int(self.state[":WAV:POIN"])
            gain = self.state[f":CHAN{ch}:SCAL"] / 7500
            origin = round(self.state[f":CHAN{ch}:OFFS"] / gain)
            if payload == ":WAV:PRE?":
                fields = raw.decode("ascii").strip().split(",")
                fields[8] = str(origin)
                return (",".join(fields) + "\n").encode("ascii")
            if payload == ":WAV:DATA?":
                codes = 32768 + origin + 4500 * np.sin(np.linspace(0, 4 * np.pi, n) + ch * 0.4)
                return ieee_block(codes.astype("<u2").tobytes()) + b"\n"
            return raw

    rows = []
    with TemporaryDirectory(prefix="dho924-ui-") as directory:
        config = ConnectionConfig("mock://ui-review", "rigol.dho924", "mock")
        runtime = ApplicationRuntime((config,), root=Path(directory), hardware=False)
        peer = Curves()
        for ch, (div, offset) in enumerate(((0.02, 0.03), (0.05, 0.025), (0.1, -0.08), (0.2, -0.44)), 1):
            peer.state[f":CHAN{ch}:SCAL"] = div
            peer.state[f":CHAN{ch}:OFFS"] = offset
        runtime.sessions.factory = lambda cfg: DHO924Adapter(peer, profile(), cfg)
        key = runtime.add(config)
        runtime.connect(key).result(5)
        window = RealWindow(runtime)
        window.show()
        window.navigate("实时监测", "oscilloscope", key)
        page = window.stack.currentWidget()
        try:
            pump(1.5)
            page.start()
            pump(0.8)
            assert page.plot.displayed_frame is not None
            for width, height in ((1280, 760), (1440, 900)):
                window.resize(width, height)
                pump(0.3)
                assert all(len(c.getData()[0]) > 0 for c in page.plot.curves.values())
                name = f"dho924-{width}x{height}-{scale}.png"
                assert window.grab().save(str(output / name))
                rows.append(name)
            page.timing_action.trigger()
            pump(0.2)
            assert window.grab().save(str(output / f"dho924-timing-{scale}.png"))
            runtime.monitoring.set_recording(key, True)
            pump(0.5)
            assert not page.memory_action.isEnabled()
            assert window.grab().save(str(output / f"dho924-recording-{scale}.png"))
            runtime.monitoring.stop(key).result(10)
            pump(0.2)
            page.pin()
            window.navigate("实时监测")
            pump(0.5)
            assert window.grab().save(str(output / f"dho924-overview-{scale}.png"))
        finally:
            window.timer.stop()
            window.controller.close()
            runtime.close()
            window.closing = True
            window.close()
            window.bridge.worker.shutdown()
    (output / f"render-{scale}.json").write_text(
        json.dumps({"hardware": False, "screens": rows, "scale": scale}), encoding="utf-8"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scale", default="1")
    args = parser.parse_args()
    render(args.output, args.scale)
