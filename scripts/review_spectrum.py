"""Render only the isolated Mock workspace; never creates a VISA transport."""

import json
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from lab_control.app import load_stylesheet, load_ui_font
from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.spectrum import SpectrumMarker
from lab_control.ui.pages.monitoring.spectrum_markers import MarkerDialog
from lab_control.ui.real_window import RealWindow


def main():
    output = Path("design/reviews/spectrum")
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setFont(QFont(load_ui_font(), 10))
    app.setStyleSheet(load_stylesheet())
    with tempfile.TemporaryDirectory(prefix="spectrum-review-") as folder:
        runtime = ApplicationRuntime(root=Path(folder), hardware=False)
        key = runtime.add(ConnectionConfig("mock://spectrum-review", "rigol.dsa815", "mock"))
        runtime.connect(key).result()
        window = RealWindow(runtime)
        window.show()
        window.navigate("实时监测", "spectrum_analyzer", key)
        page = window.stack.currentWidget()
        page.markers = [SpectrumMarker(1, 0.85e6), SpectrumMarker(2, 1e6, True, 1e5)]
        page.plot.set_markers(page.markers)
        page.toggle_monitoring()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and (page.plot.trace is None or len(page.controls.current()) != 11):
            QTest.qWait(40)
        assert page.plot.trace is not None and len(page.controls.current()) == 11
        metrics = []
        for width, height in ((1440, 900), (1280, 760), (1024, 600)):
            window.setMinimumSize(0, 0)
            window.resize(width, height)
            window.fit_to_screen(QRect(0, 0, width, height))
            QTest.qWait(250)
            window.grab().save(str(output / f"spectrum-{width}x{height}.png"))
            viewport = page.controls.scroller.viewport()
            for name in ("start_frequency", "stop_frequency", "reference_level", "attenuation", "rbw", "vbw"):
                field = page.controls.editors[name].editors["value"]
                assert field.unit.mapTo(viewport, QPoint(0, 0)).x() + field.unit.width() <= viewport.width()
            metrics.append(
                dict(
                    work_area=[width, height],
                    window=[window.width(), window.height()],
                    plot=[page.plot.width(), page.plot.height()],
                    controls_width=page.controls.width(),
                    scroll_max=page.controls.scroller.verticalScrollBar().maximum(),
                    sidebar=window.sidebar.width(),
                    apply_visible=page.controls.apply_button.visibleRegion().boundingRect().height(),
                )
            )
        # Deliberately overlapping markers and a pending draft exercise the dense states.
        window.setMinimumSize(0, 0)
        window.resize(1440, 900)
        page.markers.append(SpectrumMarker(3, 1.001e6))
        page.plot.set_markers(page.markers)
        field = page.controls.editors["reference_level"].editors["value"]
        field.input.setFocus()
        field.input.selectAll()
        QTest.keyClicks(field.input, "-15")
        QTest.qWait(200)
        window.grab().save(str(output / "spectrum-draft-markers.png"))
        dialog = MarkerDialog(page.markers[1], page)
        dialog.show()
        QTest.qWait(150)
        dialog.grab().save(str(output / "marker-editor.png"))
        dialog.close()
        (output / "layout-metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
        window.controller.close()
        window.timer.stop()
        runtime.close()
        window.closing = True
        window.bridge.worker.shutdown()
        window.close()


if __name__ == "__main__":
    main()
