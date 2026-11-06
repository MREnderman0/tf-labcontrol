"""Local-only authority administration inside the approved card workspace."""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lab_control.agent.server import saved_settings
from lab_control.application.agents.policy import COMMAND_CLIENT, INTERNAL_CLIENT
from lab_control.application.config import config_from_record
from lab_control.application.controls import OPERATION_LABELS
from lab_control.instruments.registry import describe
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox, choose_item

ACTION_LABELS = {
    "view": "查看设备与任务",
    "connect": "连接设备",
    "disconnect": "断开空闲设备",
    "read_parameters": "读取设备参数",
    "apply": "修改选定参数",
    "acquire": "单次采集并保存",
    "monitor": "持续监测",
    "record": "连续记录",
    "stop": "停止共享任务",
    "results": "获取实验文件",
    "process": "计算指标及运行采数处理流程",
    "acquisition_setup": "允许采集格式初始化",
    "output_trigger": "允许输出、触发及深存储动作",
}
STATE_LABELS = {
    "accepted": "已接受",
    "running": "运行中",
    "stopping": "停止并收尾",
    "binary_saved": "原始数据已保存",
    "converting": "CSV 转换中",
    "completed": "完成",
    "partial": "部分完成",
    "failed": "失败",
    "cancelled": "已取消",
    "interrupted": "中断，需核对",
}


class GrantDialog(QDialog):
    def __init__(self, service, parent=None, template=None):
        super().__init__(parent)
        self.service = service
        self.grant_id = ""
        self.setWindowTitle("实验范围授权")
        self.resize(860, 680)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.device = WorkspaceComboBox()
        for record in service.runtime.inventory.all():
            identity = record.get("identity", {})
            label = record.get("displayName") or identity.get("model") or record.get("adapterId") or "未识别"
            self.device.addItem(f"{label} · {identity.get('serial') or '序列号待核对'}", record["id"])
        form.addRow("设备", self.device)
        self.channels = QLineEdit("1")
        form.addRow("允许通道（逗号分隔）", self.channels)
        layout.addLayout(form)
        grid = QGridLayout()
        self.action_checks = {}
        for index, (key, label) in enumerate(ACTION_LABELS.items()):
            check = QCheckBox(label)
            check.setChecked(
                key
                in {"view", "connect", "read_parameters", "acquire", "monitor", "record", "stop", "results"}
            )
            self.action_checks[key] = check
            grid.addWidget(check, index // 3, index % 3)
        layout.addLayout(grid)
        note = QLabel("按参数能力授权，同一能力的字段一起选择。可收窄数值范围；留空边界沿用型号范围。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.parameters = QTableWidget(0, 6)
        self.parameters.setHorizontalHeaderLabels(["允许写入", "参数", "字段", "单位", "下限", "上限"])
        self.parameters.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.parameters, 1)
        self.asset = QCheckBox("已在本机核对设备铭牌与连接映射（无可读序列号时必选）")
        layout.addWidget(self.asset)
        output_note = QLabel("授权无时间限制。撤销授权会停止对应采集并完成保存；设备输出须另行控制。")
        output_note.setWordWrap(True)
        layout.addWidget(output_note)
        self.error = QLabel()
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("授予实验权限")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.device.currentIndexChanged.connect(self.load_parameters)
        self.rows: list[tuple[str, str, bool]] = []
        self.load_parameters()
        self.parameters.itemChanged.connect(self.select_operation)
        if template:
            self.device.setCurrentIndex(self.device.findData(next(iter(template["devices"]))))
            self.channels.setText(",".join(map(str, template["channels"])))
            for key, check in self.action_checks.items():
                check.setChecked(key in template["actions"])
            for row, (operation, parameter, _numeric) in enumerate(self.rows):
                if operation in template["operations"]:
                    item = self.parameters.item(row, 0)
                    assert item is not None
                    item.setCheckState(Qt.CheckState.Checked)
                    bounds = template["limits"].get(operation, {}).get(parameter)
                    if bounds:
                        for column, value in zip((4, 5), bounds):
                            item = self.parameters.item(row, column)
                            assert item is not None
                            item.setText(str(value))
            # Asset confirmation is deliberately renewed against the current device.

    def select_operation(self, item):
        if item.column() != 0 or item.row() >= len(self.rows):
            return
        operation = self.rows[item.row()][0]
        self.parameters.blockSignals(True)
        try:
            for row, (name, _field, _numeric) in enumerate(self.rows):
                if name == operation:
                    selected = self.parameters.item(row, 0)
                    assert selected is not None
                    selected.setCheckState(item.checkState())
        finally:
            self.parameters.blockSignals(False)

    def load_parameters(self):
        self.rows = []
        self.parameters.setRowCount(0)
        device = self.device.currentData()
        record = next((r for r in self.service.runtime.inventory.all() if r["id"] == device), None)
        if not record:
            return
        snapshot = self.service.runtime.sessions.capability_snapshot(device)
        try:
            schemas = list(snapshot.channels.values()) if snapshot else [describe(config_from_record(record))]
            capabilities = {
                cap.name: cap
                for schema in schemas
                for cap in schema.operations
                if cap.writable and cap.available
            }
            for operation, cap in capabilities.items():
                for name, parameter in cap.parameters.items() or [("", None)]:
                    row = self.parameters.rowCount()
                    self.parameters.insertRow(row)
                    allowed = QTableWidgetItem("")
                    allowed.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                    allowed.setCheckState(Qt.CheckState.Unchecked)
                    self.parameters.setItem(row, 0, allowed)
                    for col, text in (
                        (1, OPERATION_LABELS.get(operation, operation)),
                        (2, name),
                        (3, parameter.unit if parameter else ""),
                    ):
                        item = QTableWidgetItem(text)
                        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                        self.parameters.setItem(row, col, item)
                    numeric = parameter is not None and parameter.kind in {"number", "integer", "array"}
                    for col in (4, 5):
                        value = (parameter.minimum if col == 4 else parameter.maximum) if numeric else None
                        item = QTableWidgetItem(str(value) if value is not None else "")
                        if not numeric:
                            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                        self.parameters.setItem(row, col, item)
                    self.rows.append((operation, name, numeric))
            self.error.clear()
        except Exception as exc:
            self.error.setText(str(exc))

    def save(self):
        try:
            operations: set[str] = set()
            limits: dict[str, dict[str, list[float]]] = {}
            for row, (operation, parameter, numeric) in enumerate(self.rows):
                selected = self.parameters.item(row, 0)
                assert selected is not None
                if selected.checkState() != Qt.CheckState.Checked:
                    continue
                operations.add(operation)
                low_item, high_item = self.parameters.item(row, 4), self.parameters.item(row, 5)
                assert low_item is not None and high_item is not None
                low, high = low_item.text().strip(), high_item.text().strip()
                if numeric and (low or high):
                    if not low or not high:
                        raise ValueError("自定义范围请同时填写上下限；也可同时留空沿用型号范围")
                    limits.setdefault(operation, {})[parameter] = [float(low), float(high)]
            self.grant_id = self.service.policy.create_grant(
                INTERNAL_CLIENT,
                [self.device.currentData()],
                [key for key, check in self.action_checks.items() if check.isChecked()],
                channels=[int(value.strip()) for value in self.channels.text().replace("，", ",").split(",")],
                operations=sorted(operations),
                limits=limits,
                asset_confirmed=self.asset.isChecked(),
            )
            self.accept()
        except Exception as exc:
            self.error.setText(str(exc))


class AgentCollaborationPanel(QWidget):
    completed = Signal(str, object)

    def __init__(self, runtime, parent=None):
        super().__init__(parent)
        self.runtime, self.service = runtime, runtime.agent
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-settings")
        self.job = None
        self._signature = None
        layout = QVBoxLayout(self)
        heading = QLabel("Agent 协作实验")
        heading.setObjectName("panelTitle")
        layout.addWidget(heading)
        description = QLabel("内部 Agent 免令牌接入，共用桌面签发的实验授权。设备操作按会话串行执行。")
        description.setObjectName("pageDescription")
        description.setWordWrap(True)
        layout.addWidget(description)
        saved = saved_settings(runtime.repository)
        row = QHBoxLayout()
        self.enabled = QCheckBox("启用 Agent 接入")
        self.enabled.setChecked(saved.get("enabled", False))
        row.addWidget(self.enabled)
        row.addWidget(QLabel("本机端口"))
        self.local_port = QSpinBox()
        self.local_port.setRange(1024, 65535)
        self.local_port.setValue(saved.get("local_port", 8765))
        row.addWidget(self.local_port)
        row.addStretch(1)
        apply = QPushButton("应用接入设置")
        apply.clicked.connect(self.configure)
        row.addWidget(apply)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.lan_enabled = QCheckBox("开放可信实验网 HTTPS（共享授权）")
        self.lan_enabled.setChecked(saved.get("lan_enabled", False))
        row.addWidget(self.lan_enabled)
        self.lan_address = QLineEdit(saved.get("lan_address", ""))
        self.lan_address.setPlaceholderText("实验网卡的本机 IPv4 地址")
        row.addWidget(self.lan_address, 1)
        self.lan_port = QSpinBox()
        self.lan_port.setRange(1024, 65535)
        self.lan_port.setValue(saved.get("lan_port", 8766))
        row.addWidget(self.lan_port)
        layout.addLayout(row)
        self.local_commands = QCheckBox("启用临时命令处理（本机及实验网，退出后关闭）")
        self.local_commands.setToolTip("允许所有可访问 MCP 的客户端运行处理命令，无需实验授权。")
        layout.addWidget(self.local_commands)
        self.network_status = QLabel()
        self.network_status.setWordWrap(True)
        layout.addWidget(self.network_status)
        buttons = QGridLayout()
        for index, (label, callback) in enumerate(
            (
                ("授予实验范围", self.add_grant),
                ("一键全部授权", self.add_full_grant),
                ("导出连接配置", self.export_connection),
                ("导出 Codex 配置", self.export_codex),
            )
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            buttons.addWidget(button, index // 3, index % 3)
        layout.addLayout(buttons)
        self.grants = WorkspaceComboBox()
        self.grants.setPlaceholderText("选择实验授权")
        row = QHBoxLayout()
        row.addWidget(self.grants, 1)
        revoke = QPushButton("撤销所选实验授权")
        revoke.clicked.connect(self.revoke_grant)
        row.addWidget(revoke)
        renew = QPushButton("按所选范围重新授权")
        renew.clicked.connect(self.renew_grant)
        row.addWidget(renew)
        firewall = QPushButton("实验网防火墙…")
        firewall.clicked.connect(self.firewall)
        row.addWidget(firewall)
        layout.addLayout(row)
        self.tasks = QTableWidget(0, 5)
        self.tasks.setHorizontalHeaderLabels(["时间", "来源", "设备", "操作", "状态 / 结果"])
        self.tasks.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tasks.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.tasks.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.tasks.setMinimumHeight(170)
        self.tasks.setMaximumHeight(260)
        layout.addWidget(self.tasks)
        stop = QPushButton("停止所选 Agent 任务并完成保存")
        stop.clicked.connect(self.stop_selected)
        layout.addWidget(stop)
        self.status = QLabel("能访问接口的程序可使用共享授权。一键全部授权包含参数写入、输出和触发。")
        self.status.setWordWrap(True)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.completed.connect(self._completed)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()
        if self.enabled.isChecked():
            QTimer.singleShot(0, self.configure)

    def _server(self):
        if self.service.server is None:
            from lab_control.agent.server import AgentServer

            self.service.server = AgentServer(self.service)
        return self.service.server

    def _run(self, label, callback):
        if self.job and not self.job.done():
            self.status.setText("正在处理上一项设置，请稍候")
            return
        self.status.setText(label + "…")
        self.job = self.worker.submit(callback)

        def finish(future):
            try:
                value = future.result()
            except Exception as exc:
                value = exc
            self.completed.emit(label, value)

        self.job.add_done_callback(finish)

    def _completed(self, label, value):
        if self.runtime.closed:
            return
        self.status.setText(str(value) if isinstance(value, Exception) else label + "完成")
        self.refresh()

    def configure(self):
        if self.runtime.closed or self.service.closing:
            return
        settings = {
            "enabled": self.enabled.isChecked(),
            "local_port": self.local_port.value(),
            "lan_enabled": self.lan_enabled.isChecked(),
            "lan_address": self.lan_address.text().strip(),
            "lan_port": self.lan_port.value(),
            "local_commands": self.local_commands.isChecked(),
        }
        self._run("应用接入设置", lambda: self._server().configure(settings))

    def add_grant(self):
        dialog = GrantDialog(self.service, self)
        dialog.exec()
        dialog.deleteLater()
        self.refresh()

    def add_full_grant(self, *, template=None):
        try:
            grant_id, granted, skipped = self.service.grant_all_devices(template)
        except Exception as exc:
            self.status.setText(str(exc))
            return
        self.refresh()
        self.grants.setCurrentIndex(self.grants.findData(grant_id))
        message = f"已全部授权 {granted} 台设备，有效至手动撤销或软件退出"
        if skipped:
            message += f"。跳过 {skipped} 台身份待核对、已变化或型号不受支持的设备"
        self.status.setText(message)

    def renew_grant(self):
        template = self.service.policy.grants.get(self.grants.currentData())
        if template:
            if template.get("full_device_access"):
                self.add_full_grant(template=template)
                return
            dialog = GrantDialog(self.service, self, template=template)
            dialog.exec()
            dialog.deleteLater()
            self.refresh()

    def revoke_grant(self):
        if self.grants.currentData():
            try:
                self.service.policy.revoke_grant(self.grants.currentData())
            except Exception as exc:
                self.status.setText("已在本次运行中撤销；持久保存失败：" + str(exc))
            finally:
                self.service.wake.set()
                self.refresh()

    def export_connection(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "保存连接配置", "lab-control-connection.json", "JSON (*.json)"
        )
        if path:
            try:
                self._server().export_connection(Path(path), lan=self.lan_enabled.isChecked())
                self.status.setText("已导出连接配置；局域网使用时一并复制连接器目录")
            except Exception as exc:
                self.status.setText(str(exc))

    def export_codex(self):
        connection, _ = QFileDialog.getOpenFileName(self, "选择已导出的连接配置", "", "JSON (*.json)")
        if not connection:
            return
        destination, _ = QFileDialog.getSaveFileName(
            self, "保存 Codex 配置片段", "lab-control-codex.toml", "TOML (*.toml)"
        )
        if not destination:
            return
        if getattr(sys, "frozen", False):
            command = str(Path(sys.executable).with_name("TF-LabControl-Agent.exe"))
            args = ["--agent-connector", "--connection", connection]
        else:
            command = sys.executable
            args = ["-m", "lab_control", "--agent-connector", "--connection", connection]
        text = (
            "[mcp_servers.lab_control]\n"
            + "command = "
            + json.dumps(command)
            + "\n"
            + "args = "
            + json.dumps(args)
            + "\n"
            + "startup_timeout_sec = 30\ntool_timeout_sec = 90\n"
        )
        try:
            with Path(destination).open("x", encoding="utf-8") as stream:
                stream.write(text)
            self.status.setText("已导出配置片段；远程电脑须按其连接器及配置文件位置调整路径")
        except Exception as exc:
            self.status.setText(str(exc))

    def firewall(self):
        from lab_control.agent.firewall import launch_firewall

        choice, accepted = choose_item(
            self,
            "实验网防火墙",
            "仅管理本软件指定网卡、端口和程序的规则",
            ["添加允许规则", "撤销本软件规则"],
        )
        if accepted:
            settings = dict(self._server().settings)
            self._run(
                "维护防火墙",
                lambda: launch_firewall(self.runtime, settings, remove=choice.startswith("撤销")),
            )

    def stop_selected(self):
        row = self.tasks.currentRow()
        item = self.tasks.item(row, 0) if row >= 0 else None
        if item is not None:
            try:
                self.service.stop_local(item.data(Qt.ItemDataRole.UserRole))
                self.status.setText("已请求停止；等待记录收尾，设备输出不自动改变")
            except Exception as exc:
                self.status.setText(str(exc))

    def refresh(self):
        if self.runtime.closed or self.service.closing:
            return
        snapshot = self.service.snapshot()
        server = snapshot["server"]
        settings = server.get("settings", {})
        self.network_status.setText(
            (
                f"正在监听：127.0.0.1:{settings.get('local_port')} /mcp"
                + (
                    f" · HTTPS {settings['lan_address']}:{settings['lan_port']} /mcp"
                    if settings.get("lan_enabled")
                    else ""
                )
                + (" · 临时命令处理已启用" if server.get("local_commands") else "")
            )
            if server["running"]
            else "接入未运行" + ("：" + server["error"] if server.get("error") else "")
        )
        grants = snapshot["grants"]
        signature = tuple((g["id"], g["enabled"], g.get("full_device_access", False)) for g in grants)
        if signature != self._signature:
            selected_grant = self.grants.currentData()
            self.grants.clear()
            for grant_id, active, full in signature:
                scope = "全部设备授权" if full else "范围授权"
                state = "有效" if active else "已失效"
                self.grants.addItem(f"{scope} · {state} · {grant_id[:8]}", grant_id)
            self.grants.setCurrentIndex(max(0, self.grants.findData(selected_grant)))
            self._signature = signature
        selected = self.tasks.item(self.tasks.currentRow(), 0)
        selected_id = selected.data(Qt.ItemDataRole.UserRole) if selected else ""
        tasks = snapshot["tasks"][:32]
        records = {r["id"]: r for r in self.runtime.inventory.all()}
        sources = {INTERNAL_CLIENT: "内部 MCP", COMMAND_CLIENT: "MCP 临时命令"}
        self.tasks.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            record = records.get(task["device_id"], {})
            values = (
                task.get("created_at", ""),
                sources.get(task["client_id"], task["client_id"][:8]),
                record.get("displayName") or record.get("identity", {}).get("model", task["device_id"][:8]),
                ACTION_LABELS.get(task["action"], task["action"]),
                STATE_LABELS.get(task["state"], task["state"])
                + ("：" + task["error"] if task["error"] else ""),
            )
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                item.setData(Qt.ItemDataRole.UserRole, task["id"])
                self.tasks.setItem(row, col, item)
            if task["id"] == selected_id:
                self.tasks.selectRow(row)

    def shutdown(self):
        self.worker.shutdown(wait=True)
