"""Render the real scope workspace with isolated Mock sessions and no hardware."""

import argparse
import json
import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory


def render(output: Path, scale: str) -> None:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_SCALE_FACTOR"] = scale
    from PySide6.QtCore import QPoint, QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication, QInputDialog

    from lab_control.app import load_stylesheet, load_ui_font
    from lab_control.application.config import ConnectionConfig
    from lab_control.application.runtime import ApplicationRuntime
    from lab_control.ui.real_window import RealWindow

    app = QApplication([])
    app.setStyle("Fusion")
    app.setFont(QFont(load_ui_font(), 10))
    app.setStyleSheet(load_stylesheet())
    output.mkdir(parents=True, exist_ok=True)
    evidence = []

    def pump(seconds=0.05):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.002)

    def wait(predicate):
        end = time.monotonic() + 10
        while not predicate() and time.monotonic() < end:
            pump(0.01)
        assert predicate()

    with TemporaryDirectory(prefix="scope-timing-review-") as temporary:
        runtime = ApplicationRuntime(root=Path(temporary), hardware=False)
        key = runtime.add(ConnectionConfig("mock://scope-review", "lecroy.9404m", "mock"))
        runtime.repository.save_workspace(
            f"monitor-timing/electronic/{key}", dict(cadence="fastest", interval=8)
        )
        runtime.connect(key).result(5)
        window = RealWindow(runtime)
        window.show()
        window.navigate("实时监测", "oscilloscope", key)
        page = window.stack.currentWidget()
        try:
            wait(lambda: page.controls and len(page.controls.values) >= 30 and not page.controls.running)
            page.timing_action.setChecked(True)
            assert page.timing.options()["cadence"] == "interval" and page.timing.options()["interval"] == 0.5
            page.start()
            wait(lambda: page.plot.displayed_frame is not None)
            for width, height in ((1280, 760), (1440, 900)):
                window.resize(width, height)
                pump(0.15)
                for widget in (page.controls.points, page.timing.cadence, page.timing.interval, page.actual):
                    position = widget.mapTo(window, QPoint(0, 0))
                    assert 0 <= position.x() <= width - widget.width()
                    assert 0 <= position.y() <= height - widget.height()
                assert page.actual.toolTip() == ""
                prefix = f"scope-{width}x{height}-{scale}"
                assert window.grab().save(str(output / (prefix + ".png")))
                page.controls.points.showPopup()
                pump()
                assert page.controls.points.view().window().grab().save(str(output / (prefix + "-menu.png")))
                page.controls.points.hidePopup()
                seen = []

                def capture_dialog():
                    dialog = app.activeModalWidget()
                    assert isinstance(dialog, QInputDialog)
                    assert dialog.grab().save(str(output / (prefix + "-custom.png")))
                    seen.append(True)
                    dialog.reject()

                QTimer.singleShot(50, capture_dialog)
                page.controls.points.choose(page.controls.points.findData("custom"))
                assert seen and not page.controls.running
                evidence.append(
                    dict(
                        logical_size=[width, height],
                        dpr=window.devicePixelRatioF(),
                        cadence=page.timing.options(),
                        dialog_cancel_zero_write=True,
                    )
                )
        finally:
            window.timer.stop()
            window.controller.close()
            runtime.close()
            window.closing = True
            window.close()
            window.bridge.worker.shutdown()
    (output / f"render-{scale}.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("build/scope-timing-review"))
    parser.add_argument("--scale", default="1")
    args = parser.parse_args()
    render(args.output, args.scale)
