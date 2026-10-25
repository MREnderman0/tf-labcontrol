"""Capture parameter typography using the real UI and isolated Mock services."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLineEdit  # noqa: E402

from lab_control.app import load_stylesheet, load_ui_font  # noqa: E402
from lab_control.application.config import ConnectionConfig  # noqa: E402
from lab_control.application.runtime import ApplicationRuntime  # noqa: E402
from lab_control.ui.pages.device_control.editors import ParameterEditor  # noqa: E402
from lab_control.ui.real_window import RealWindow  # noqa: E402

MODELS = ("rs.sma100b", "tek.afg31252", "siglent.sdg2082x")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--models", nargs="+", choices=MODELS, default=MODELS)
    parser.add_argument("--expect-dpr", type=float, required=True)
    args = parser.parse_args()
    args.destination.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    app.setStyle("Fusion")
    app.setFont(QFont(load_ui_font(), 10))
    app.setStyleSheet(load_stylesheet())
    metrics = []
    writes = []
    with TemporaryDirectory(prefix="lab-parameter-type-") as directory:
        runtime = ApplicationRuntime((), root=Path(directory), hardware=False)
        window = RealWindow(runtime)
        window.resize(1440, 1000)
        window.show()

        def settle() -> None:
            for _ in range(100):
                QTest.qWait(30)
                if not window.controller.jobs:
                    QTest.qWait(100)
                    return
            raise RuntimeError("Mock jobs did not settle")

        def capture(model: str, state: str) -> None:
            settle()
            page = window.stack.currentWidget()
            page.scroll_area.verticalScrollBar().setValue(0)
            QTest.qWait(50)
            filename = f"{model}-{state}.png"
            ratio = window.devicePixelRatioF()
            assert abs(ratio - args.expect_dpr) < 1e-8, (ratio, args.expect_dpr)
            fields = []
            for field in page.findChildren(ParameterEditor):
                if not isinstance(field.input, QLineEdit) or not field.isVisible():
                    continue
                fields.append(
                    {
                        "kind": field.spec.kind,
                        "placeholder": field.input.placeholderText(),
                        "text": field.input.text(),
                        "font_px": field.input.font().pixelSize(),
                        "weight": field.input.font().weight(),
                        "height": field.input.height(),
                        "unit_height": field.unit.height(),
                        "unit_font_px": field.unit.font().pixelSize(),
                        "source_value": field.input.objectName() == "sourceValue",
                        "placeholder_fits": field.input.fontMetrics().horizontalAdvance(
                            field.input.placeholderText()
                        )
                        <= field.input.contentsRect().width() - 20,
                    }
                )
            window.grab().save(str(args.destination / filename))
            metrics.append(
                {
                    "file": filename,
                    "model": model,
                    "state": state,
                    "page": type(page).__name__,
                    "connected": window.controller.has_connected_session(page.device_id),
                    "surface": "primary_controls",
                    "numeric_fields": sum(f["kind"] in {"number", "integer"} for f in fields),
                    "platform": app.platformName(),
                    "device_pixel_ratio": ratio,
                    "window": window.size().toTuple(),
                    "fields": fields,
                }
            )

        try:
            for model in args.models:
                key = runtime.add(ConnectionConfig(f"mock://typography/{model}", model, "mock"))
                runtime.connect(key).result(5)
                runtime.sessions.sessions[key].adapter.apply = writes.append
                window.controller.poll()
                window.navigate("设备管理", "signal_generator", key)
                settle()
                page = window.stack.currentWidget()
                for _ in range(100):
                    window.controller.poll()
                    QTest.qWait(50)
                    field = page.panels[1].editors["frequency"].editors["value"]
                    if field.input.text():
                        break
                assert field.input.text(), "Mock frequency did not populate"
                QTest.qWait(400)
                capture(model, "readback")
                field.input.setFocus()
                field.input.selectAll()
                QTest.keyClick(field.input, Qt.Key.Key_Backspace)
                capture(model, "empty")
                QTest.keyClicks(field.input, "2")
                capture(model, "editing")
            assert not writes, writes
            (args.destination / "metrics.json").write_text(
                json.dumps(
                    {"hardware": False, "parameter_writes": len(writes), "captures": metrics},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            print(
                f"Saved {len(metrics)} Mock captures; DPR {args.expect_dpr}; parameter writes: {len(writes)}"
            )
        finally:
            window.timer.stop()
            window.controller.close()
            runtime.close()
            window.closing = True
            window.bridge.worker.shutdown()
            window.close()


if __name__ == "__main__":
    main()
