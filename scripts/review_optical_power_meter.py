"""Render the optical power workspace at the supported review sizes.

This is an offline visual fixture. It connects only to the isolated JW8105 Mock
adapter and writes a new review batch; historical screenshots are preserved.
"""

from __future__ import annotations

import argparse
import math
import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lab_control.app import load_stylesheet, load_ui_font
from lab_control.application.config import ConnectionConfig
from lab_control.application.monitoring_state import MonitorFrame, MonitorTask
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest, Measurement, MeasurementBatch, Quality
from lab_control.ui.pages.monitoring.optical_power import OpticalPowerMonitoringPage
from lab_control.ui.real_window import RealWindow


def checkbox_gallery(output: Path, app: QApplication) -> None:
    gallery = QWidget()
    layout = QVBoxLayout(gallery)
    layout.addWidget(QLabel("圆形多选控件 · 普通 / 表格 / 树列表"))
    checks = QHBoxLayout()
    for name, checked, enabled in (
        ("未选", False, True),
        ("已选", True, True),
        ("禁用未选", False, False),
        ("禁用已选", True, False),
    ):
        check = QCheckBox(name)
        check.setChecked(checked)
        check.setEnabled(enabled)
        checks.addWidget(check)
    partial = QCheckBox("半选")
    partial.setTristate(True)
    partial.setCheckState(Qt.CheckState.PartiallyChecked)
    checks.addWidget(partial)
    layout.addLayout(checks)
    table = QTableWidget(2, 1)
    table.setHorizontalHeaderLabels(["表格勾选"])
    table.horizontalHeader().setStretchLastSection(True)
    tree = QTreeWidget()
    tree.setHeaderLabels(["树列表勾选"])
    for index, state in enumerate((Qt.CheckState.Unchecked, Qt.CheckState.Checked)):
        item = QTableWidgetItem(f"通道 {index + 1}")
        item.setCheckState(state)
        table.setItem(index, 0, item)
        row = QTreeWidgetItem([f"组件 {index + 1}"])
        row.setCheckState(0, state)
        tree.addTopLevelItem(row)
    layout.addWidget(table)
    layout.addWidget(tree)
    gallery.resize(680, 380)
    gallery.show()
    app.processEvents()
    gallery.grab().save(str(output / "round-checkboxes.png"))
    gallery.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("tmp/reviews/optical-power-30min"))
    args = parser.parse_args()
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setFont(load_ui_font())
    app.setStyleSheet(load_stylesheet())
    runtime = ApplicationRuntime(root=output / "runtime", hardware=False)
    device = runtime.add(ConnectionConfig("mock://visual-jw8105", "joinwit.jw8105", "mock"))
    runtime.connect(device).result(5)
    selected = [ch for ch in range(1, 17) if ch != 8]
    runtime.repository.save_workspace(
        "monitoring_channels/optical_power_meter", {f"device:{device}": selected}
    )
    runtime.repository.save_workspace("monitoring_display/optical_power_meter", {"unit": "W"})
    # Deterministic, simulated 30-minute history, without waiting or device reads.
    request = AcquisitionRequest("scalar", channels=tuple(range(1, 17)), points=1, quantity="power")
    task = MonitorTask((request,), "general", 0.5, running=False)
    origin = time.perf_counter() - 1800
    identity = runtime.sessions.sessions[device].identity
    for index in range(3601):
        measurements = tuple(
            Measurement(
                "power",
                "dBm",
                -10 - ch * 0.37 + 0.13 * math.sin(index / 150 + ch),
                ch,
                quality=Quality.SIMULATED,
            )
            for ch in request.channels
        )
        task.power_window.add(
            MonitorFrame(
                index + 1,
                0,
                request,
                MeasurementBatch(identity, measurements, {}),
                origin + index * 0.5,
                "visual",
            )
        )
    task.sequence = 3601
    task.power_window.advance(origin + 1800, stopped=True)
    with runtime.monitoring.lock:
        runtime.monitoring.tasks[device] = task
    window = RealWindow(runtime)
    window.controller.set_mode("optical")
    window.navigate("实时监测", "optical_power_meter", device)
    page = window.stack.currentWidget()
    if not isinstance(page, OpticalPowerMonitoringPage):
        raise RuntimeError("optical monitoring page was not created")
    window.show()
    # Drain RealWindow's deferred screen fit before applying review dimensions.
    app.processEvents()
    app.processEvents()
    for unit in ("W", "dBm"):
        if page.display_unit != unit:
            page.toggle_display_unit()
        for name, size in (("1280x760", (1280, 760)), ("1440x900", (1440, 900)), ("narrow", (900, 760))):
            window.resize(*size)
            app.processEvents()
            page.refresh()
            app.processEvents()
            for row in page.cards.values():
                for value in (row.value, row.peak):
                    assert (
                        value.fontMetrics().horizontalAdvance(value.text()) <= value.contentsRect().width()
                    ), (name, value.text(), value.width())
            assert page.readout_scroll.horizontalScrollBar().maximum() == 0
            window.grab().save(str(output / f"{name}-{unit}.png"))
    checkbox_gallery(output, app)
    window.timer.stop()
    window.controller.close()
    runtime.close()
    window.closing = True
    window.bridge.worker.shutdown()
    window.storage_panel.shutdown()
    window.environment_panel.shutdown()


if __name__ == "__main__":
    main()
