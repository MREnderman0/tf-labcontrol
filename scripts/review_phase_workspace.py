"""Render the approved desktop's phase workspace with synthetic data only."""

import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from lab_control.app import load_stylesheet, load_ui_font
from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.instruments.devices.microchip_53100a.tests.simulation import PhaseSimulation
from lab_control.ui.real_window import RealWindow


def main():
    app = QApplication([])
    app.setStyle("Fusion")
    app.setFont(QFont(load_ui_font(), 10))
    app.setStyleSheet(load_stylesheet())
    folder = Path("design/reviews/phase-workspace")
    folder.mkdir(parents=True, exist_ok=True)

    def pump(seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            app.processEvents()
            time.sleep(0.002)

    with TemporaryDirectory(prefix="phase-ui-review-") as root:
        runtime = ApplicationRuntime(root=Path(root), hardware=False)
        runtime.sessions.factory = PhaseSimulation
        key = runtime.add(
            ConnectionConfig("mock://phase-review", "microchip.53100a", "mock", usb_instance_id="TEST-USB")
        )
        runtime.connect(key).result(3)
        window = RealWindow(runtime)
        window.show()
        window.navigate("实时监测", "phase_noise_analyzer", key)
        page = window.stack.currentWidget()
        try:
            pump(0.5)
            window.resize(1440, 900)
            pump(0.1)
            assert window.grab().save(str(folder / "phase-ready.png"))
            page.start()
            pump(0.6)
            assert window.grab().save(str(folder / "phase-collecting.png"))
            deadline = time.monotonic() + 20
            while not page.outcomes.get(key, "").startswith("采集完成") and time.monotonic() < deadline:
                pump(0.05)
            assert page.outcomes[key].startswith("采集完成"), page.outcomes
            for width, height in ((1440, 900), (1280, 760)):
                window.resize(width, height)
                pump(0.1)
                assert window.grab().save(str(folder / f"phase-results-{width}.png"))
            print("Rendered ready, acquiring and completed synthetic phase workspace")
        finally:
            window.controller.close()
            window.closing = True
            window.close()
            window.bridge.worker.shutdown()
            runtime.close()


if __name__ == "__main__":
    main()
