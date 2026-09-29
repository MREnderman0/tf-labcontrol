"""Render the real power workspace with isolated Mock sessions, without hardware."""

import argparse
import json
import os
import time
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory


def render(output: Path, scale: str) -> None:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_SCALE_FACTOR"] = scale
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    from lab_control.app import load_stylesheet, load_ui_font
    from lab_control.application.config import ConnectionConfig
    from lab_control.application.runtime import ApplicationRuntime
    from lab_control.ui.real_window import RealWindow

    app = QApplication([])
    app.setStyle("Fusion")
    app.setFont(QFont(load_ui_font(), 10))
    app.setStyleSheet(load_stylesheet())
    output.mkdir(parents=True, exist_ok=True)

    def pump(seconds=0.1):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.002)

    def wait(predicate):
        deadline = time.monotonic() + 10
        while not predicate() and time.monotonic() < deadline:
            pump(0.02)
        assert predicate()

    evidence = []
    with TemporaryDirectory(prefix="power-workspace-review-") as temporary:
        runtime = ApplicationRuntime(root=Path(temporary), hardware=False)
        key = runtime.add(ConnectionConfig("mock://power-review", "gwinstek.gpd4303s", "mock"))
        runtime.connect(key).result(5)
        adapter = runtime.sessions.sessions[key].adapter
        adapter.values["1.all_outputs"] = {"value": True}
        for ch in range(1, 5):
            adapter.values[f"{ch}.voltage"] = {"value": ch * 0.8}
            adapter.values[f"{ch}.current"] = {"value": ch * 0.05}
            adapter.values[f"{ch}.output"] = {"value": True}
        window = RealWindow(runtime)
        window.show()
        window.navigate("设备管理", "power_supply", key)
        page = window.stack.currentWidget()
        try:
            wait(lambda: all(page.controls.current_settings(ch) for ch in (1, 2, 3, 4)))
            page.start()
            wait(lambda: page.plot.displayed_frame is not None)
            window.resize(1440, 900)
            pump()
            assert page.plot.time_origin is not None
            assert page.plot.plots["voltage"].viewRange()[0] == [0, 600]
            assert window.grab().save(str(output / f"power-start-{scale}.png"))
            # Synthetic Mock-only history exercises the ten-minute viewport and
            # separated units without waiting ten minutes or opening hardware.
            import math

            template = page.plot.displayed_frame
            page.plot.reset()
            for index in range(121):
                points = tuple(
                    replace(m, values=[m.values[0] * (1 + 0.025 * math.sin(index / 8 + m.channel))])
                    for m in template.batch.measurements
                )
                synthetic = replace(
                    template,
                    sequence=-121 + index,
                    received_at=template.received_at - 600 + 5 * index,
                    batch=replace(template.batch, measurements=points),
                )
                page.plot.show_frame(synthetic, page.channels())
            for width, height in ((1280, 760), (1440, 900)):
                window.resize(width, height)
                pump()
                controls = page.controls
                editor = controls.panels[1].editors["voltage"]
                page.selector.combo.setFocus()
                pump()
                editor.dirty = False
                editor.reconcile_reading()
                pump()
                controls.scroller.verticalScrollBar().setValue(0)
                prefix = f"power-{width}x{height}-{scale}"
                assert controls.scroller.horizontalScrollBar().maximum() == 0
                assert page.timing.actual.toolTip() == ""
                assert page.timing.isHidden() and page.actual.isVisible()
                assert page.timing.cadence.isHidden() and page.timing.interval.isHidden()
                for widget in (page.actual, page.start_button, controls.output.button):
                    point = widget.mapTo(window, QPoint(0, 0))
                    assert 0 <= point.x() <= width - widget.width()
                    assert 0 <= point.y() <= height - widget.height()
                assert window.grab().save(str(output / (prefix + ".png")))
                page.timing_action.setChecked(True)
                pump()
                assert window.grab().save(str(output / (prefix + "-interval.png")))
                page.timing.presets.showPopup()
                pump()
                popup = page.timing.presets.view().window()
                assert popup.screen().availableGeometry().contains(popup.frameGeometry())
                assert popup.width() >= page.timing.presets.width()
                assert page.timing.presets.view().window().grab().save(str(output / (prefix + "-menu.png")))
                page.timing.presets.hidePopup()
                page.timing_action.setChecked(False)
                field = editor.editors["value"].input
                field.setFocus()
                field.setText("2.7")
                field.textEdited.emit("2.7")
                pump(1.1)
                assert field.hasFocus() and field.text() == "2.7" and editor.dirty
                assert window.grab().save(str(output / (prefix + "-draft.png")))
                controls.scroller.verticalScrollBar().setValue(
                    controls.scroller.verticalScrollBar().maximum()
                )
                pump()
                assert window.grab().save(str(output / (prefix + "-bottom.png")))
                controls.scroller.verticalScrollBar().setValue(0)
                evidence.append(
                    dict(
                        logical_size=[width, height],
                        dpr=window.devicePixelRatioF(),
                        options=page.timing.options(),
                        no_horizontal_scroll=True,
                        popup_within_work_area=True,
                        timing_collapsed_by_default=True,
                        time_axis="elapsed_from_first_valid_frame",
                        draft_focus_preserved=True,
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
    parser.add_argument("--output", type=Path, default=Path("build/power-workspace-review"))
    parser.add_argument("--scale", default="1")
    args = parser.parse_args()
    render(args.output, args.scale)
