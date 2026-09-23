"""Capture actual Qt widgets and popup surfaces using isolated Mock sessions only."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QPoint, QRect  # noqa: E402
from PySide6.QtGui import QFont, QPainter  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from lab_control.app import load_stylesheet, load_ui_font  # noqa: E402
from lab_control.application.config import ConnectionConfig  # noqa: E402
from lab_control.application.runtime import ApplicationRuntime  # noqa: E402
from lab_control.ui.pages.device_control.editors import ParameterEditor  # noqa: E402
from lab_control.ui.real_window import RealWindow  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, nargs="?", default=Path(".tools/popup-review"))
    parser.add_argument("--screen", type=int, nargs=2, default=(1600, 1000))
    parser.add_argument("--window", type=int, nargs=2)
    args = parser.parse_args()
    destination = args.destination
    destination.mkdir(parents=True, exist_ok=True)
    qt_args = []
    if os.environ["QT_QPA_PLATFORM"] == "offscreen":
        config = destination / "screen.json"
        config.write_text(
            json.dumps(
                {
                    "screens": [
                        {
                            "name": "review",
                            "width": args.screen[0],
                            "height": args.screen[1],
                            "logicalDpi": 96,
                            "logicalBaseDpi": 96,
                            "dpr": 1,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        # Qt separates platform options on ':', including a Windows drive colon.
        qt_args = ["popup-review", "-platform", f"offscreen:configfile={os.path.relpath(config)}"]
    app = QApplication(qt_args)
    app.setStyle("Fusion")
    app.setFont(QFont(load_ui_font(), 10))
    app.setStyleSheet(load_stylesheet())
    metrics = []
    with TemporaryDirectory(prefix="lab-popup-review-") as temporary:
        runtime = ApplicationRuntime((), root=Path(temporary), hardware=False)
        ids = {}
        for name, model in (("awg", "tek.afg31252"), ("rf", "rs.sma100b")):
            key = runtime.add(
                ConnectionConfig(f"mock://review/{name}", model, "mock", identity_confirmed=True)
            )
            runtime.connect(key).result()
            ids[name] = key
        window = RealWindow(runtime)
        window.show()

        def settle() -> None:
            for _ in range(100):
                QTest.qWait(50)
                if not window.controller.jobs:
                    QTest.qWait(50)
                    return
            raise RuntimeError("Mock readback did not settle")

        try:
            settle()
            sizes = [args.window] if args.window else ((1440, 900), (1100, 760), (1024, 600), (800, 560))
            for width, height in sizes:
                window.resize(width, height)
                if width <= 1024:
                    window.fit_to_screen(QRect(0, 0, width, height))
                for kind in ("amplitude", "frequency", "waveform", "time", "rf-frequency"):
                    window.navigate(
                        "设备管理", "signal_generator", ids["rf" if kind.startswith("rf-") else "awg"]
                    )
                    settle()
                    page = window.stack.currentWidget()
                    page.scroll_area.verticalScrollBar().setValue(0)
                    if kind == "time":
                        field = next(f for f in page.tabs.findChildren(ParameterEditor) if f.spec.unit == "s")
                        for index in range(page.tabs.count()):
                            if page.tabs.widget(index).isAncestorOf(field):
                                page.tabs.setCurrentIndex(index)
                                break
                        settle()
                        page.scroll_area.ensureWidgetVisible(field)
                        combo = field.unit
                    else:
                        field = page.panels[1].editors[kind.removeprefix("rf-")].editors["value"]
                        combo = field.input if kind == "waveform" else field.unit
                        page.scroll_area.ensureWidgetVisible(field)
                    QTest.qWait(100)
                    combo.showPopup()
                    QTest.qWait(100)
                    view = combo.view()
                    popup = view.window()
                    # QWidget.grab excludes separate popup windows. Composite the two Qt
                    # captures at their real logical coordinates; do not synthesize controls.
                    canvas = window.grab()
                    painter = QPainter(canvas)
                    painter.drawPixmap(window.mapFromGlobal(popup.mapToGlobal(QPoint())), popup.grab())
                    painter.end()
                    filename = f"{kind}-{width}.png"
                    canvas.save(str(destination / filename))
                    metrics.append(
                        {
                            "file": filename,
                            "window": window.size().toTuple(),
                            "device_pixel_ratio": window.devicePixelRatioF(),
                            "items": [combo.itemText(i) for i in range(combo.count())],
                            "row_heights": [view.sizeHintForRow(i) for i in range(combo.count())],
                            "viewport": view.viewport().size().toTuple(),
                            "popup": popup.geometry().getRect(),
                            "screen": combo.screen().availableGeometry().getRect(),
                        }
                    )
                    combo.hidePopup()
            (destination / "metrics.json").write_text(
                json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(f"Saved {len(metrics)} Mock popup captures to {destination}")
        finally:
            window.timer.stop()
            window.controller.close()
            runtime.close()
            window.closing = True
            window.bridge.worker.shutdown()
            window.close()


if __name__ == "__main__":
    main()
