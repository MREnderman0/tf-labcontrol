"""Capture actual Qt widgets and popup surfaces using isolated Mock sessions only."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QPoint, QRect, Qt  # noqa: E402
from PySide6.QtGui import QFont, QPainter, QPixmap  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QFrame,
    QLabel,
    QStyle,
    QStyleOptionComboBox,
    QVBoxLayout,
)

from lab_control.app import load_stylesheet, load_ui_font  # noqa: E402
from lab_control.application.config import ConnectionConfig  # noqa: E402
from lab_control.application.runtime import ApplicationRuntime  # noqa: E402
from lab_control.domain.instruments import Parameter  # noqa: E402
from lab_control.ui.pages.device_control.editors import ParameterEditor  # noqa: E402
from lab_control.ui.real_window import RealWindow  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, nargs="?", default=Path(".tools/popup-review"))
    parser.add_argument("--screen", type=int, nargs=2, default=(1600, 1000))
    parser.add_argument("--window", type=int, nargs=2)
    parser.add_argument("--dpr", type=float, default=1)
    parser.add_argument(
        "--kinds",
        nargs="+",
        choices=("device", "amplitude", "frequency", "waveform", "time", "rf-frequency"),
        default=("device", "amplitude", "frequency", "waveform", "time", "rf-frequency"),
    )
    args = parser.parse_args()
    os.environ["QT_SCALE_FACTOR"] = str(args.dpr)
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
                            "width": int(args.screen[0] * args.dpr),
                            "height": int(args.screen[1] * args.dpr),
                            "logicalDpi": 96,
                            "logicalBaseDpi": 96,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        # Qt separates platform options on ':', including a Windows drive colon.
        qt_args = ["popup-review", "-platform", f"offscreen:configfile={os.path.relpath(config)}"]
    app = QApplication(qt_args)
    if app.platformName() == "offscreen":
        assert app.devicePixelRatio() == args.dpr
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
                for kind in args.kinds:
                    window.controller.set_mode("optical" if kind == "time" else "electronic")
                    window.navigate(
                        "设备管理", "signal_generator", ids["rf" if kind.startswith("rf-") else "awg"]
                    )
                    settle()
                    page = window.stack.currentWidget()
                    page.scroll_area.verticalScrollBar().setValue(0)
                    if kind == "device":
                        combo = page.selector.combo
                    elif kind == "time":
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
                        if kind == "amplitude":
                            combo.setCurrentIndex(combo.findText("mVpp"))
                    QTest.qWait(100)
                    combo.showPopup()
                    QTest.qWait(100)
                    view = combo.view()
                    popup = view.window()
                    # QWidget.grab excludes separate popup windows. Composite the two Qt
                    # captures at their real logical coordinates; do not synthesize controls.
                    # A valid popup may extend beyond the application window.
                    # Include both surfaces rather than clipping the evidence.
                    window_rect = QRect(window.mapToGlobal(QPoint()), window.size())
                    popup_rect = QRect(popup.mapToGlobal(QPoint()), popup.size())
                    bounds = window_rect.united(popup_rect)
                    ratio = window.devicePixelRatioF()
                    canvas = QPixmap(math.ceil(bounds.width() * ratio), math.ceil(bounds.height() * ratio))
                    canvas.setDevicePixelRatio(ratio)
                    canvas.fill(Qt.GlobalColor.transparent)
                    painter = QPainter(canvas)
                    painter.drawPixmap(window_rect.topLeft() - bounds.topLeft(), window.grab())
                    painter.drawPixmap(popup_rect.topLeft() - bounds.topLeft(), popup.grab())
                    painter.end()
                    filename = f"{kind}-{width}.png"
                    canvas.save(str(destination / filename))
                    option = QStyleOptionComboBox()
                    combo.initStyleOption(option)
                    text_rect = combo.style().subControlRect(
                        QStyle.ComplexControl.CC_ComboBox,
                        option,
                        QStyle.SubControl.SC_ComboBoxEditField,
                        combo,
                    )
                    metrics.append(
                        {
                            "file": filename,
                            "platform": app.platformName(),
                            "window": window.size().toTuple(),
                            "capture_bounds": bounds.getRect(),
                            "device_pixel_ratio": window.devicePixelRatioF(),
                            "closed_text_rect": text_rect.getRect(),
                            "selected_text_width": combo.fontMetrics().horizontalAdvance(combo.currentText()),
                            "items": [combo.itemText(i) for i in range(combo.count())],
                            "row_heights": [view.sizeHintForRow(i) for i in range(combo.count())],
                            "viewport": view.viewport().size().toTuple(),
                            "popup": popup.geometry().getRect(),
                            "screen": combo.screen().availableGeometry().getRect(),
                        }
                    )
                    combo.hidePopup()
            # Closed unit controls use the same compact style and actual font as the signal panels.
            gallery = QFrame()
            gallery.setObjectName("compactOperation")
            gallery_layout = QVBoxLayout(gallery)
            gallery_layout.addWidget(QLabel("单位完整显示 · 模拟参数"))
            fields = []
            for unit, selected in (
                ("Vpp", "mVpp"),
                ("V/div", "mV/div"),
                ("V/div", "V/div"),
                ("Hz", "MHz"),
                ("mV RMS/√Hz", "mV RMS/√Hz"),
            ):
                field = ParameterEditor(Parameter(unit=unit), gallery)
                field.unit.setCurrentIndex(field.unit.findText(selected))
                gallery_layout.addWidget(field)
                fields.append(field)
            for width in (520, 300):
                gallery.resize(width, 500)
                gallery.show()
                QTest.qWait(60)
                filename = f"units-{width}.png"
                gallery.grab().save(str(destination / filename))
                for field in fields:
                    combo = field.unit
                    option = QStyleOptionComboBox()
                    combo.initStyleOption(option)
                    text_rect = combo.style().subControlRect(
                        QStyle.ComplexControl.CC_ComboBox,
                        option,
                        QStyle.SubControl.SC_ComboBoxEditField,
                        combo,
                    )
                    text_width = combo.fontMetrics().horizontalAdvance(combo.currentText())
                    assert text_rect.width() >= text_width + 4
                    metrics.append(
                        {
                            "file": filename,
                            "unit": combo.currentText(),
                            "device_pixel_ratio": gallery.devicePixelRatioF(),
                            "window": gallery.size().toTuple(),
                            "closed_text_rect": text_rect.getRect(),
                            "text_width": text_width,
                        }
                    )
            gallery.close()
            window.navigate("设备管理", "power_supply", "missing-review-device")
            QTest.qWait(50)
            window.grab().save(str(destination / "disconnected-empty.png"))
            metrics.append(
                {
                    "file": "disconnected-empty.png",
                    "window": window.size().toTuple(),
                    "device_pixel_ratio": window.devicePixelRatioF(),
                }
            )
            (destination / "metrics.json").write_text(
                json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(f"Saved {len({item['file'] for item in metrics})} Mock Qt captures to {destination}")
        finally:
            window.timer.stop()
            window.controller.close()
            runtime.close()
            window.closing = True
            window.bridge.worker.shutdown()
            window.close()


if __name__ == "__main__":
    main()
