"""Render the shared overview/dialog using temporary simulated inventory only."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from lab_control.app import load_stylesheet, load_ui_font  # noqa: E402
from lab_control.application.config import ConnectionConfig  # noqa: E402
from lab_control.application.runtime import ApplicationRuntime  # noqa: E402
from lab_control.discovery.service import DiscoveryCandidate  # noqa: E402
from lab_control.domain.instruments import DeviceIdentity  # noqa: E402
from lab_control.ui.real_window import RealWindow  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, nargs="?", default=Path(".tools/device-pool-review"))
    parser.add_argument("--dpr", type=float, default=1)
    args = parser.parse_args()
    if args.dpr <= 0:
        parser.error("--dpr must be positive")
    os.environ["QT_SCALE_FACTOR"] = str(args.dpr)
    args.destination.mkdir(parents=True, exist_ok=True)
    qt_args = []
    if os.environ["QT_QPA_PLATFORM"] == "offscreen":
        screen = args.destination / "screen.json"
        screen.write_text(
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
        qt_args = ["device-pool-review", "-platform", f"offscreen:configfile={os.path.relpath(screen)}"]
    app = QApplication(qt_args)
    assert app.devicePixelRatio() == args.dpr
    app.setStyle("Fusion")
    app.setFont(QFont(load_ui_font(), 10))
    app.setStyleSheet(load_stylesheet())
    metrics = []
    with TemporaryDirectory(prefix="device-pool-review-") as temporary:
        runtime = ApplicationRuntime(root=Path(temporary), hardware=False)
        config = ConnectionConfig("mock://current", "rigol.dg5504pro", "mock")
        key = runtime.add(config)
        runtime.connect(key).result()
        runtime.inventory.edit(key, display_name="实验信号源 · 模拟")
        for resource, adapter, manufacturer, model, name in (
            ("mock://bench-b", "rigol.dg5504pro", "RIGOL", "DG5504 Pro", "备用函数信号源 · 模拟"),
            ("mock://bench-c", "siglent.sdg2082x", "SIGLENT", "SDG2082X", "调制信号源 · 模拟"),
        ):
            candidate = ConnectionConfig(resource, adapter, "mock")
            record = runtime.inventory.merge(
                DiscoveryCandidate(candidate, "configured"),
                "review",
                DeviceIdentity(manufacturer, model, "REVIEW-" + resource),
                adapter,
            )
            runtime.inventory.edit(record["id"], display_name=name)
        old = ConnectionConfig("mock://history", "rs.sma100b", "mock")
        identity = DeviceIdentity("Rohde&Schwarz", "SMA100B", "REVIEW-FIXTURE")
        runtime.inventory.merge(DiscoveryCandidate(old, "configured"), "old", identity, old.adapter)
        runtime.inventory.merge(
            DiscoveryCandidate(old, "configured"),
            "latest",
            error="离线测试：设备未接入",
            reason="probe_failed",
        )
        window = RealWindow(runtime)
        window.timer.stop()
        try:
            window.show()
            for width, height in ((1440, 900), (1000, 650)):
                window.fit_to_screen(QRect(0, 0, width, height))
                window.resize(width - 32, height - 60)
                app.processEvents()
                window.open_category("signal_generator")
                app.processEvents()
                for scope in ("current", "history"):
                    window.presence_filter.setCurrentIndex(window.presence_filter.findData(scope))
                    app.processEvents()
                    path = args.destination / f"pool-{width}-{scope}.png"
                    assert window.grab().save(str(path))
                    rect = window.device_pool.panel.geometry()
                    metrics.append(
                        {
                            "image": str(path),
                            "pool": [rect.x(), rect.y(), rect.width(), rect.height()],
                            "overview": [window.overview_page.width(), window.overview_page.height()],
                            "visibleRows": sum(not row.isHidden() for row in window.rows.values()),
                            "summary": window.summary.text(),
                            "dpr": window.devicePixelRatioF(),
                            "horizontalScroll": window.device_pool.scroll_area.horizontalScrollBar().maximum(),
                        }
                    )
                window.device_pool.scroll_area.ensureWidgetVisible(window.details.values["error"])
                app.processEvents()
                assert window.grab().save(str(args.destination / f"pool-{width}-discovery.png"))
                window.pool_menu.popup(window.pool_more.mapToGlobal(window.pool_more.rect().topLeft()))
                app.processEvents()
                assert window.pool_menu.grab().save(str(args.destination / f"pool-{width}-menu.png"))
                window.pool_menu.close()
                window.presence_filter.setCurrentIndex(0)
            window.navigate("设备管理", "signal_generator")
            app.processEvents()
            assert window.device_pool.isHidden()
            assert window.grab().save(str(args.destination / "category-page-pool-closed.png"))
        finally:
            window.closing = True
            window.hide()
            window.bridge.worker.shutdown()
            runtime.close()
    (args.destination / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(metrics, ensure_ascii=True))


if __name__ == "__main__":
    main()
