"""Render the real Qt electronics UI at a requested DPI using isolated Mock sessions."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QFont, QPainter  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from lab_control.app import load_stylesheet, load_ui_font  # noqa: E402
from lab_control.application.config import ConnectionConfig  # noqa: E402
from lab_control.application.runtime import ApplicationRuntime  # noqa: E402
from lab_control.ui.real_window import RealWindow  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dpr", type=float, default=1)
    parser.add_argument("--destination", type=Path, default=Path("design/reviews/electronic-signal-source"))
    args = parser.parse_args()
    folder = args.destination / f"dpi-{round(args.dpr * 100)}"
    folder.mkdir(parents=True, exist_ok=True)
    os.environ["QT_SCALE_FACTOR"] = str(args.dpr)
    config = folder / "screen.json"
    config.write_text(
        json.dumps(
            {
                "screens": [
                    {
                        "name": "review",
                        "width": int(1600 * args.dpr),
                        "height": int(1000 * args.dpr),
                        "logicalDpi": 96,
                        "logicalBaseDpi": 96,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    app = QApplication(["review", "-platform", f"offscreen:configfile={os.path.relpath(config)}"])
    assert app.devicePixelRatio() == args.dpr
    app.setStyle("Fusion")
    app.setFont(QFont(load_ui_font(), 10))
    app.setStyleSheet(load_stylesheet())
    metrics = []
    with TemporaryDirectory(prefix="lab-electronics-review-") as temporary:
        runtime = ApplicationRuntime(root=Path(temporary), hardware=False)
        ids = {}
        for kind, model in (("dual", "siglent.sdg2082x"), ("four", "rigol.dg5504pro"), ("rf", "rs.sma100b")):
            key = runtime.add(ConnectionConfig(f"mock://review/{kind}", model, "mock"))
            runtime.connect(key).result()
            ids[kind] = key
        window = RealWindow(runtime)
        window.show()
        window.activateWindow()

        def wait_for(predicate):
            for _ in range(100):
                QTest.qWait(50)
                if predicate():
                    QTest.qWait(100)
                    return
            raise RuntimeError("Mock UI did not settle")

        def save(name, overlay=None):
            image = window.grab()
            if overlay is not None:
                painter = QPainter(image)
                painter.drawPixmap(
                    overlay.mapToGlobal(QPoint(0, 0)) - window.mapToGlobal(QPoint(0, 0)), overlay.grab()
                )
                painter.end()
            image.save(str(folder / f"{name}.png"))

        try:
            QTest.qWait(200)
            for width, height in ((1280, 760), (1440, 900)):
                window.showNormal()
                window.resize(width, height)
                window.move(0, 0)
                for kind in ids:
                    window.navigate("设备管理", "signal_generator", ids[kind])
                    page = window.stack.currentWidget()
                    wait_for(lambda: all("正常" in o.state.text() for o in page.outputs.values()))
                    page.scroll_area.verticalScrollBar().setValue(0)
                    QTest.qWait(100)
                    assert window.size().toTuple() == (width, height)
                    assert page.last_columns == (1 if kind == "rf" else 2)
                    assert page.scroll_area.horizontalScrollBar().maximum() == 0
                    metrics.append(
                        {
                            "size": [width, height],
                            "dpr": args.dpr,
                            "kind": kind,
                            "cards": [c.geometry().getRect() for c in page.channel_cards],
                            "scroll": page.scroll_area.verticalScrollBar().maximum(),
                        }
                    )
                    save(f"{kind}-{width}")
                    if kind == "four":
                        page.scroll_area.verticalScrollBar().setValue(
                            page.scroll_area.verticalScrollBar().maximum()
                        )
                        QTest.qWait(100)
                        save(f"four-bottom-{width}")
                    if kind == "dual":
                        field = page.panels[1].editors["frequency"].editors["value"]
                        window.activateWindow()
                        field.input.setFocus()
                        QTest.qWait(50)
                        assert field.input.hasFocus()
                        save(f"focus-{width}")
                        combo = page.panels[1].editors["waveform"].editors["value"].input
                        combo.showPopup()
                        QTest.qWait(80)
                        assert window.screen().availableGeometry().contains(combo.view().window().geometry())
                        save(f"waveform-{width}", combo.view().window())
                        combo.hidePopup()
                        page.open_settings()
                        dialog = page.settings_dialog
                        wait_for(lambda: dialog.current["reference_clock"] is not None)
                        dialog.choices["reference_clock"]["EXT"].click()
                        dialog.activateWindow()
                        dialog.apply_button.setFocus()
                        QTest.qWait(50)
                        assert dialog.apply_button.hasFocus()
                        assert window.screen().availableGeometry().contains(dialog.geometry())
                        save(f"settings-{width}", dialog)
                        dialog.grab().save(str(folder / f"dialog-{width}.png"))
                        QTest.keyClick(dialog, Qt.Key.Key_Escape)
                        wait_for(lambda: page.settings_dialog is None)
            window.navigate("设备管理", "signal_generator", ids["dual"])
            window.showMaximized()
            wait_for(
                lambda: all("正常" in o.state.text() for o in window.stack.currentWidget().outputs.values())
            )
            save("maximized")
        finally:
            window.timer.stop()
            window.controller.close()
            runtime.close()
            window.closing = True
            window.close()
            window.bridge.worker.shutdown()
    (folder / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
