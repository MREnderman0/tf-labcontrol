"""Render isolated Mock UI previews; this script never opens physical transports."""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from lab_control.app import load_stylesheet, load_ui_font  # noqa: E402
from lab_control.application.config import ConnectionConfig  # noqa: E402
from lab_control.application.runtime import ApplicationRuntime  # noqa: E402
from lab_control.ui.real_window import RealWindow  # noqa: E402


def main() -> None:
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".tools/signal-ui-review")
    destination.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    app.setStyle("Fusion")
    app.setFont(QFont(load_ui_font(), 10))
    app.setStyleSheet(load_stylesheet())
    with TemporaryDirectory(prefix="lab-ui-review-") as directory:
        root = Path(directory)
        runtime = ApplicationRuntime((), root=root / "mock", hardware=False)
        devices = {}
        for name, adapter in (
            ("rf", "rs.sma100b"),
            ("awg", "tek.afg31252"),
            ("four-channel", "rigol.dg5504pro"),
        ):
            config = ConnectionConfig(f"mock://review/{name}", adapter, "mock", identity_confirmed=True)
            key = runtime.add(config)
            runtime.connect(key).result()
            if name == "rf":
                # Representative UI fixture only, never an instrument acceptance record.
                adapter = runtime.sessions.sessions[key].adapter
                adapter.values.update(
                    {
                        "1.frequency": {"value": 102e6},
                        "1.power": {"value": 21},
                        "1.reference_clock": {"value": "EXT"},
                    }
                )
                bounds = {
                    "frequency": (8e3, 12.75e9),
                    "sweep_start": (8e3, 12.75e9),
                    "sweep_stop": (8e3, 12.75e9),
                    "power": (-145, 40),
                }
                adapter.caps = replace(
                    adapter.caps,
                    operations=tuple(
                        replace(
                            cap,
                            parameters={
                                "value": replace(
                                    cap.parameters["value"],
                                    minimum=bounds[cap.name][0],
                                    maximum=bounds[cap.name][1],
                                    range_source="instrument",
                                )
                            },
                        )
                        if cap.name in bounds
                        else cap
                        for cap in adapter.caps.operations
                    ),
                )
                adapter.capability_details = {"rf_output_limit_dbm": 20, "power_offset_db": 0}
                runtime.sessions.submit(key, "refresh_capabilities").result()
            devices[name] = key
        window = RealWindow(runtime)
        window.show()

        def settle() -> None:
            QTest.qWait(100)
            for _ in range(100):
                if not window.controller.jobs:
                    QTest.qWait(50)
                    return
                QTest.qWait(50)
            raise RuntimeError("Mock readback did not finish before screenshot")

        try:
            settle()
            for width, height in ((1440, 900), (1100, 760)):
                for name in ("rf", "awg"):
                    window.navigate("设备管理", "signal_generator", devices[name])
                    window.resize(width, height)
                    settle()
                    window.grab().save(str(destination / f"{name}-{width}-100.png"))
                    print(name, 100, window.size().toTuple(), flush=True)
            window.resize(1440, 900)
            window.navigate("设备管理", "signal_generator", devices["awg"])
            settle()
            page = window.stack.currentWidget()
            editor = page.panels[1].editors["amplitude"]
            field = editor.editors["value"]
            field.input.setFocus()
            field.input.setText("NaN")
            editor.save_draft()
            field.finish_editing(force=True)
            QTest.qWait(100)
            window.grab().save(str(destination / "invalid-restored.png"))
            field.input.setText("2")
            editor.save_draft()
            field.finish_editing(force=True)
            settle()
            window.grab().save(str(destination / "pending-draft.png"))
            editor.editors["value"].input.setText("1")
            editor.save_draft()
            editor.editors["value"].finish_editing(force=True)
            editor.editors["value"].feedback.clear()
            for width, height in ((1366, 728), (1024, 600), (800, 560)):
                window.resize(1440, 900)
                window.fit_to_screen(QRect(0, 0, width, height))
                settle()
                page.scroll_area.verticalScrollBar().setValue(0)
                window.grab().save(str(destination / f"awg-workarea-{width}x{height}.png"))
            window.resize(1440, 900)
            window.navigate("设备管理", "signal_generator", devices["four-channel"])
            settle()
            window.grab().save(str(destination / "four-channel.png"))
            window.resize(960, 600)
            QTest.qWait(300)
            window.grab().save(str(destination / "four-channel-narrow.png"))
            window.resize(1440, 900)
            window.navigate("设备管理", "signal_generator", devices["rf"])
            page = window.stack.currentWidget()
            for index in range(page.tabs.count()):
                if page.tabs.tabText(index) == "调制":
                    page.tabs.setCurrentIndex(index)
            settle()
            page.scroll_area.verticalScrollBar().setValue(page.scroll_area.verticalScrollBar().maximum())
            window.grab().save(str(destination / "modulation-unavailable.png"))
        finally:
            window.timer.stop()
            window.controller.close()
            runtime.close()
            window.closing = True
            window.bridge.worker.shutdown()
            window.close()


if __name__ == "__main__":
    main()
