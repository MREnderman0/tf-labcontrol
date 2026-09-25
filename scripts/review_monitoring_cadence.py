"""Render the real monitoring page using isolated Mock data, never hardware."""

import argparse
import json
import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory


def render(scale: str, output: Path) -> None:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_SCALE_FACTOR"] = scale
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication, QDoubleSpinBox

    from lab_control.app import load_stylesheet, load_ui_font
    from lab_control.application.config import ConnectionConfig
    from lab_control.application.runtime import ApplicationRuntime
    from lab_control.ui.real_window import RealWindow

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

    rows = []
    with TemporaryDirectory(prefix="monitor-cadence-review-") as root:
        runtime = ApplicationRuntime(root=Path(root), hardware=False)
        key = runtime.add(ConnectionConfig("mock://cadence-review", "rigol.dsa815", "mock"))
        runtime.connect(key).result(3)
        adapter = runtime.sessions.sessions[key].adapter
        acquire = adapter.acquire

        def paced(request):
            time.sleep(0.03)
            return acquire(request)

        adapter.acquire = paced
        window = RealWindow(runtime)
        window.show()
        window.navigate("实时监测", "spectrum_analyzer", key)
        page = window.stack.currentWidget()
        page.toggle_monitoring()
        try:
            pump(0.8)
            assert page.plot.displayed_frame is not None
            for cadence in ("fastest", "interval"):
                page.timing.cadence.setCurrentIndex(page.timing.cadence.findData(cadence))
                for width, height in ((1280, 760), (1440, 900)):
                    window.resize(width, height)
                    pump(0.65)
                    fields = [page.timing.cadence, page.interval, page.timing.actual]
                    centers = [f.geometry().center().y() for f in fields]
                    assert max(centers) - min(centers) <= 2
                    assert len(page.timing.findChildren(QDoubleSpinBox)) == 1
                    assert page.timing.actual.toolTip() == ""
                    assert window.width() == width and window.height() == height
                    for widget in fields:
                        position = widget.mapTo(window, QPoint(0, 0))
                        assert position.x() >= 0 and position.x() + widget.width() <= width
                    name = f"spectrum-{cadence}-{width}x{height}-{scale}.png"
                    assert window.grab().save(str(output / name))
                    rows.append(
                        dict(
                            file=name,
                            logical_size=[width, height],
                            dpr=window.devicePixelRatioF(),
                            cadence=cadence,
                            one_row=True,
                            numeric_fields=1,
                            tooltip="",
                            options=page.timing.options(),
                            y_range=page.plot.plot.viewRange()[1],
                            save_label=page.save_action.text(),
                        )
                    )
            assert not (Path(root) / "data").exists()
        finally:
            window.timer.stop()
            window.controller.close()
            runtime.close()
            window.closing = True
            window.close()
            window.bridge.worker.shutdown()
    (output / f"render-{scale}.json").write_text(
        json.dumps({"hardware": False, "renders": rows}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", choices=("1", "1.25", "1.5"), default="1")
    parser.add_argument("--output", type=Path, default=Path("design/reviews/unified-monitoring"))
    args = parser.parse_args()
    render(args.scale, args.output)
