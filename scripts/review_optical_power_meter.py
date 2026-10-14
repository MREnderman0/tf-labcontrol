"""Render the optical power workspace at the supported review sizes.

This is an offline visual fixture. It connects only to the isolated JW8105 Mock
adapter and writes screenshots under ``design/reviews/optical-power``.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from lab_control.app import load_stylesheet, load_ui_font
from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.ui.pages.monitoring.optical_power import OpticalPowerMonitoringPage
from lab_control.ui.real_window import RealWindow


def main() -> None:
    output = Path("design/reviews/optical-power")
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setFont(load_ui_font())
    app.setStyleSheet(load_stylesheet())
    runtime = ApplicationRuntime(root=Path("tmp/visual-optical-power"), hardware=False)
    device = runtime.add(ConnectionConfig("mock://visual-jw8105", "joinwit.jw8105", "mock"))
    runtime.connect(device).result(5)
    window = RealWindow(runtime)
    window.controller.set_mode("optical")
    window.navigate("实时监测", "optical_power_meter", device)
    page = window.stack.currentWidget()
    if not isinstance(page, OpticalPowerMonitoringPage):
        raise RuntimeError("optical monitoring page was not created")
    window.show()
    for name, size in (("1280x760", (1280, 760)), ("1440x900", (1440, 900)), ("narrow", (900, 760))):
        window.resize(*size)
        app.processEvents()
        window.grab().save(str(output / f"{name}.png"))
    window.controller.close()
    runtime.close()
    window.bridge.worker.shutdown()


if __name__ == "__main__":
    main()
