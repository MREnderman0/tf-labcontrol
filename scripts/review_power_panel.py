"""Offline visual evidence for all-channel supply controls; never opens VISA."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from lab_control.app import load_stylesheet, load_ui_font
from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.ui.real_window import RealWindow


def settle(app, seconds=0.3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def main():
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setFont(QFont(load_ui_font(), 10))
    app.setStyleSheet(load_stylesheet())
    destination = Path("design/reviews/power-supply")
    destination.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix="power-panel-review-") as tmp:
        runtime = ApplicationRuntime(root=Path(tmp), hardware=False)
        keys = {}
        for adapter in ("gwinstek.gpd4303s", "itech.it6322"):
            key = runtime.add(ConnectionConfig("mock://review/" + adapter, adapter, "mock"))
            runtime.connect(key).result()
            instrument = runtime.sessions.sessions[key].adapter
            for channel in instrument.caps.channels:
                instrument.values[f"{channel}.voltage"] = {"value": (3.3, 12.0, 2.5, 1.8)[channel - 1]}
                instrument.values[f"{channel}.current"] = {"value": (0.5, 0.1, 0.25, 0.1)[channel - 1]}
            keys[adapter] = key
        window = RealWindow(runtime)
        window.show()
        settle(app)
        try:
            for adapter, key in keys.items():
                window.navigate("设备管理", "power_supply", key)
                settle(app, 1.5)
                page = window.stack.currentWidget()
                for width, height in ((1440, 900), (1280, 760), (1024, 600)):
                    area = QRect(0, 0, width, height)
                    window.fit_to_screen(area)
                    window.resize(width, height)
                    window.fit_to_screen(area)
                    settle(app)
                    name = f"{adapter}-{width}x{height}.png"
                    window.grab().save(str(destination / name))
                    viewport = page.scroll_area.viewport()
                    entry = {
                        "model": adapter,
                        "work_area": [width, height],
                        "window": [window.frameGeometry().width(), window.frameGeometry().height()],
                        "sidebar": window.sidebar.width(),
                        "screenshot": name,
                        "horizontal_scroll": page.scroll_area.horizontalScrollBar().maximum(),
                        "vertical_scroll": page.scroll_area.verticalScrollBar().maximum(),
                        "cards": {},
                    }
                    for channel, panel in page.panels.items():
                        for operation, editor in panel.editors.items():
                            field = editor.editors["value"]
                            measured = panel.measurements[operation]
                            assert field.input.geometry().bottom() < measured.y(), (
                                adapter,
                                width,
                                channel,
                                operation,
                                field.input.geometry(),
                                measured.geometry(),
                                field.size(),
                                field.minimumSizeHint(),
                                field.heightForWidth(field.width()),
                            )
                        button = panel.apply_button
                        rect = QRect(button.mapTo(viewport, QPoint(0, 0)), button.size())
                        entry["cards"][channel] = {
                            "size": [panel.width(), panel.height()],
                            "apply_on_screen": viewport.rect().contains(rect),
                            "two_fields": panel.visible_columns == 2,
                        }
                    assert entry["horizontal_scroll"] == 0
                    assert entry["sidebar"] == 232
                    assert window.frameGeometry().width() <= width
                    assert window.frameGeometry().height() <= height
                    if width >= 1280:
                        assert all(c["apply_on_screen"] for c in entry["cards"].values()), entry
                    results.append(entry)
                    if width == 1024:
                        page.scroll_area.verticalScrollBar().setValue(
                            page.scroll_area.verticalScrollBar().maximum()
                        )
                        settle(app)
                        last = page.panels[max(page.panels)].apply_button
                        assert viewport.rect().contains(QRect(last.mapTo(viewport, QPoint()), last.size()))
                        window.grab().save(str(destination / f"{adapter}-1024x600-bottom.png"))
                        page.scroll_area.verticalScrollBar().setValue(0)
                if adapter == "gwinstek.gpd4303s":
                    window.fit_to_screen(QRect(0, 0, 1280, 760))
                    panel = page.panels[1]
                    editor = panel.editors["voltage"]
                    field = editor.editors["value"]
                    field.input.setText("4.2")
                    editor.save_draft()
                    panel.message("测试夹具：电流设置失败 · 已完成：电压 · 未执行项保留草稿")
                    field.input.setFocus()
                    settle(app)
                    assert panel.apply_button.isEnabled() and editor.readback.isVisible()
                    window.grab().save(str(destination / "gpd-draft-error-focus.png"))
            (destination / "geometry.json").write_text(
                json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        finally:
            window.timer.stop()
            window.controller.close()
            runtime.close()
            window.closing = True
            window.close()
            window.bridge.worker.shutdown()
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
