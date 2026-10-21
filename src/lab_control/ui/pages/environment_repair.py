"""Background environment inspection in the approved real workspace."""

from __future__ import annotations

import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.config import ConnectionConfig
from lab_control.deployment.environment import export_report, inspect_environment, plan_repair

LABELS = {
    "ok": "正常",
    "missing": "缺失",
    "incompatible": "不兼容",
    "unknown": "待核实",
    "not_required": "未需要",
    "error": "检查失败",
}


class InspectionSignals(QObject):
    progress = Signal(object)
    finished = Signal(object)


class EnvironmentRepairPanel(QWidget):
    def __init__(self, runtime, parent=None, *, inspector=inspect_environment):
        super().__init__(parent)
        self.runtime, self.inspector = runtime, inspector
        self.report: dict = {}
        self.cancel_event = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="environment-check")
        self.future = None
        self.disposed = False
        self.signals = InspectionSignals(self)
        self.signals.progress.connect(self.show_report)
        self.signals.finished.connect(self.completed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        title = QLabel("环境与修复")
        title.setObjectName("panelTitle")
        layout.addWidget(title)
        self.status = QLabel("检查软件运行库、设备驱动和实验网络，查看修复方案。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.check_button = QPushButton("检查环境")
        self.cancel_button = QPushButton("取消检查")
        self.plan_button = QPushButton("查看修复方案")
        self.export_button = QPushButton("导出报告")
        self.prepare_button = QPushButton("准备设备组件")
        for button in (self.check_button, self.cancel_button, self.plan_button, self.export_button):
            buttons.addWidget(button)
        buttons.addWidget(self.prepare_button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.cancel_button.setEnabled(False)
        self.plan_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.table = QTreeWidget()
        self.table.setHeaderLabels(["检查项", "状态", "影响范围", "检查结果"])
        self.table.setRootIsDecorated(False)
        self.table.setMinimumHeight(150)
        self.table.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.header().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        self.details = QTextBrowser()
        self.details.setMaximumHeight(130)
        layout.addWidget(self.details)
        note = QLabel("可按需下载组件并打开厂商安装向导；网络配置和自动修复尚未接入。")
        note.setObjectName("mutedText")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.check_button.clicked.connect(self.start)
        self.cancel_button.clicked.connect(self.cancel)
        self.plan_button.clicked.connect(self.show_plan)
        self.export_button.clicked.connect(self.choose_export)
        self.table.currentItemChanged.connect(self.show_details)
        self.prepare_button.clicked.connect(self.prepare_components)

    def prepare_components(self):
        from lab_control.ui.pages.component_preparation import show_preparation

        show_preparation(self.runtime, self)

    def start(self):
        if self.disposed or (self.future and not self.future.done()):
            return
        configs = list(self.runtime.configurations)
        for record in self.runtime.inventory.all():
            try:
                config = ConnectionConfig(**record["config"])
                if config not in configs:
                    configs.append(config)
            except (KeyError, TypeError, ValueError):
                continue
        self.cancel_event = threading.Event()
        self.report = {}
        self.table.clear()
        self.details.clear()
        self.check_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.plan_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.status.setText("正在检查环境…")
        root = self.runtime.repository.root
        lan = self.runtime.hardware and self.runtime.lan_settings.enabled

        def work():
            try:
                result = self.inspector(
                    root,
                    tuple(configs),
                    lan=lan,
                    cancel=self.cancel_event,
                    progress=self.signals.progress.emit,
                )
            except Exception as exc:
                result = {
                    "schema_version": 1,
                    "kind": "environment_inspection",
                    "state": "error",
                    "error": str(exc),
                    "executor_available": False,
                }
            self.signals.finished.emit(result)

        self.future = self.executor.submit(work)

    def cancel(self):
        self.cancel_event.set()
        self.cancel_button.setEnabled(False)
        self.status.setText("正在取消检查，已完成的结果将保留…")

    def show_report(self, report):
        if self.disposed:
            return
        self.report = copy.deepcopy(report)
        selected = self.table.currentIndex().row()
        self.table.clear()
        for item in report.get("items", []):
            row = QTreeWidgetItem(
                [
                    item["title"],
                    LABELS[item["state"]],
                    "、".join(item["affected"]) or "软件环境",
                    item["reason"],
                ]
            )
            row.setToolTip(3, item["reason"])
            self.table.addTopLevelItem(row)
        if self.table.topLevelItemCount():
            selected_row = self.table.topLevelItem(max(0, min(selected, self.table.topLevelItemCount() - 1)))
            if selected_row is not None:
                self.table.setCurrentItem(selected_row)
        self.export_button.setEnabled(bool(report.get("items")))

    def completed(self, report):
        if self.disposed:
            return
        if report.get("state") == "error":
            self.report.update(state="error", error=report.get("error"))
            report = self.report
        self.show_report(report)
        self.check_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.plan_button.setEnabled(bool(report.get("items")))
        state = report.get("state")
        self.status.setText(
            {
                "complete": "环境检查完成；修复方案尚未执行。",
                "cancelled": "检查已取消；已完成的结果已保留。",
                "error": "检查失败：" + report.get("error", "未知错误"),
            }.get(state, "检查结束")
        )

    def show_details(self, *_args):
        index = self.table.currentIndex().row()
        items = self.report.get("items", [])
        if not 0 <= index < len(items):
            return
        item = items[index]
        payload = item.get("payload", {}).get("state", "not_applicable")
        material = {
            "verified": "已校验",
            "missing": "缺失",
            "invalid": "校验失败",
            "unknown": "待补齐",
            "not_applicable": "不适用",
        }.get(payload, payload)
        self.details.setPlainText(
            f"{item['reason']}\n建议：{item['action']}\n离线材料：{material}　"
            f"管理员权限：{'需要' if item.get('requires_admin') else '不需要'}　"
            f"重启：{'可能需要' if item.get('reboot', 'no') != 'no' else '不需要'}"
        )

    def show_plan(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("环境修复方案")
        dialog.resize(780, 540)
        layout = QVBoxLayout(dialog)
        browser = QTextBrowser()
        plan = plan_repair(self.report)
        lines = [
            plan["notice"],
            "检查未完成，以下仅包含已检查项目。" if not plan["inspection_complete"] else "方案尚未执行。",
        ]
        for item in plan["actions"]:
            lines.extend(
                [
                    "",
                    item["title"],
                    item["reason"],
                    "建议：" + item["action"],
                    "离线材料：" + item["payload"]["state"],
                    f"管理员权限：{'需要' if item['requires_admin'] else '不需要'}；重启：{item['reboot']}",
                ]
            )
        if not plan["actions"]:
            lines.append("已检查项目暂无修复建议。")
        browser.setPlainText("\n".join(lines))
        layout.addWidget(browser)
        dialog.exec()

    def save_report(self, path: Path):
        value = copy.deepcopy(self.report)
        value["repair_plan"] = plan_repair(value)
        export_report(value, path)

    def choose_export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出环境检查报告", "environment-report.json", "JSON (*.json)"
        )
        if path:
            try:
                self.save_report(Path(path))
                self.status.setText("报告已导出：" + path)
            except OSError as exc:
                self.status.setText("导出失败：" + str(exc))

    def shutdown(self):
        self.disposed = True
        self.cancel_event.set()
        self.executor.shutdown(wait=True, cancel_futures=True)
