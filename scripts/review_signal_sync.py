"""Capture real Qt sync states and measure a 20-device Mock workload; no hardware I/O."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QRect, QTimer  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from lab_control.app import load_stylesheet, load_ui_font  # noqa: E402
from lab_control.application.config import ConnectionConfig  # noqa: E402
from lab_control.application.runtime import ApplicationRuntime  # noqa: E402
from lab_control.domain.instruments import AcquisitionRequest, DeviceError  # noqa: E402
from lab_control.ui.real_window import RealWindow  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, nargs="?", default=Path(".tools/signal-sync-review"))
    args = parser.parse_args()
    destination = args.destination
    destination.mkdir(parents=True, exist_ok=True)
    config = destination / "screen.json"
    config.write_text(
        json.dumps(
            {"screens": [{"width": 1600, "height": 1000, "logicalDpi": 96, "logicalBaseDpi": 96, "dpr": 1}]}
        ),
        encoding="utf-8",
    )
    app = QApplication(["sync-review", "-platform", f"offscreen:configfile={os.path.relpath(config)}"])
    app.setStyle("Fusion")
    app.setFont(QFont(load_ui_font(), 10))
    app.setStyleSheet(load_stylesheet())
    with TemporaryDirectory(prefix="lab-sync-review-") as temporary:
        runtime = ApplicationRuntime(root=Path(temporary), hardware=False)
        keys, calls, writes, active, maximum = [], [], [], {}, {}
        lock = threading.Lock()
        for index in range(20):
            model = "tek.afg31252" if index == 0 else "rs.sma100b" if index == 1 else "rigol.dg5504pro"
            key = runtime.add(ConnectionConfig(f"mock://review/{index}", model, "mock"))
            runtime.connect(key).result()
            keys.append(key)
            adapter = runtime.sessions.sessions[key].adapter
            original = adapter.read_settings

            def read(names, channel, *, original=original, index=index):
                with lock:
                    active[index] = active.get(index, 0) + 1
                    maximum[index] = max(maximum.get(index, 0), active[index])
                    calls.append((index, channel, len(names), time.monotonic()))
                try:
                    time.sleep(0.08 if index == 2 else 0.002)
                    if index == 3:
                        raise DeviceError("Mock timeout for review", "timeout")
                    return original(names, channel)
                finally:
                    with lock:
                        active[index] -= 1

            adapter.read_settings = read
            original_apply = adapter.apply
            adapter.apply = lambda command, original_apply=original_apply: (
                writes.append(command.operation),
                original_apply(command),
            )[1]
        runtime.sessions.sessions[keys[0]].adapter.values.update(
            {"1.output": {"value": 0.0}, "2.output": {"value": 1.0}}
        )
        runtime.sessions.set_control_group(keys[4], True)
        runtime.monitoring.start(
            keys[5], (AcquisitionRequest("status", (1,), quantity="frequency"),), interval=0.05
        )
        window = RealWindow(runtime)
        window.show()
        window.navigate("设备管理", "signal_generator", keys[0])
        ticks, pending_samples = [], []
        timer = QTimer(window)

        def sample():
            ticks.append(time.monotonic())
            pending_samples.extend(s["pending"] for s in runtime.sessions.snapshots())

        timer.timeout.connect(sample)
        timer.start(20)
        start = time.monotonic()
        screenshots = []

        def wait_until(predicate, timeout=8):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                QTest.qWait(20)
                if predicate():
                    return
            raise RuntimeError("Mock automatic synchronization did not settle")

        try:
            QTest.qWait(2000)
            assert not runtime.sessions.readback_frames(keys[4])
            runtime.sessions.set_control_group(keys[4], False)
            wait_until(
                lambda: all(
                    (4, "offset") in runtime.sessions.readback_frames(k)
                    for i, k in enumerate(keys)
                    if i >= 2 and i != 3
                )
            )
            QTest.qWait(2000)
            timer.stop()
            duration = time.monotonic() - start
            performance = {
                "environment": "Windows / Python 3.12 / Qt offscreen / Mock only",
                "devices": 20,
                "foreground_interval_s": 1,
                "background_interval_s": 10,
                "duration_s": round(duration, 3),
                "max_automatic_devices_in_flight": runtime.monitoring.settings_sync.max_observed_concurrent,
                "max_same_device_read_concurrency": max(maximum.values()),
                "max_pending_per_device": max(pending_samples),
                "max_operations_per_read": max(c[2] for c in calls),
                "gui_timer_interval_ms": 20,
                "gui_timer_samples": len(ticks),
                "gui_max_tick_gap_ms": round(max(b - a for a, b in zip(ticks, ticks[1:])) * 1000, 2),
                "read_batches_per_device": [sum(c[0] == i for c in calls) for i in range(20)],
                "timeout_device_read_attempts": sum(c[0] == 3 for c in calls),
                "monitor_frames": runtime.monitoring.tasks[keys[5]].sequence,
                "group_pause_respected": True,
                "automatic_writes": len(writes),
            }
            assert performance["max_automatic_devices_in_flight"] <= 4
            assert performance["max_same_device_read_concurrency"] == 1
            assert performance["max_pending_per_device"] <= 1
            assert performance["max_operations_per_read"] <= 4
            assert performance["timeout_device_read_attempts"] == 1
            assert not writes
            for width, height in ((1440, 900), (1100, 760), (1024, 600), (800, 560)):
                window.fit_to_screen(QRect(0, 0, width, height))
                window.resize(width, height)
                for index, label in ((0, "awg"), (1, "rf")):
                    window.navigate("设备管理", "signal_generator", keys[index])
                    page = window.stack.currentWidget()
                    wait_until(lambda: all("正常" in o.state.text() for o in page.outputs.values()))
                    page.scroll_area.verticalScrollBar().setValue(0)
                    QTest.qWait(100)
                    filename = f"{label}-{width}.png"
                    window.grab().save(str(destination / filename))
                    screenshots.append(
                        {
                            "file": filename,
                            "window": window.size().toTuple(),
                            "columns": page.last_columns,
                            "sidebar_width": window.sidebar.width(),
                            "status": [o.state.text() for o in page.outputs.values()],
                        }
                    )
                    if index == 0 and width <= 1024:
                        page.scroll_area.ensureWidgetVisible(page.panels[1].apply_button)
                        QTest.qWait(100)
                        window.grab().save(str(destination / f"awg-actions-{width}.png"))
            window.fit_to_screen(QRect(0, 0, 1100, 760))
            window.resize(1100, 760)
            window.navigate("设备管理", "signal_generator", keys[0])
            page = window.stack.currentWidget()
            wait_until(lambda: all("正常" in o.state.text() for o in page.outputs.values()))
            field = page.panels[1].editors["amplitude"].editors["value"]
            field.input.setFocus()
            field.input.selectAll()
            QTest.keyClicks(field.input, "2.5")
            page.selector.combo.setFocus()
            QTest.qWait(200)
            window.grab().save(str(destination / "draft-1100.png"))
            runtime.monitoring.enable_settings_sync(False)
            QTest.qWait(3200)
            window.grab().save(str(destination / "stale-1100.png"))
            (destination / "metrics.json").write_text(
                json.dumps(
                    {"performance": performance, "screenshots": screenshots}, ensure_ascii=False, indent=2
                ),
                encoding="utf-8",
            )
            print(json.dumps(performance, ensure_ascii=False, indent=2))
        finally:
            timer.stop()
            window.timer.stop()
            window.controller.close()
            runtime.close()
            window.closing = True
            window.bridge.worker.shutdown()
            window.close()


if __name__ == "__main__":
    main()
