from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.catalog import instrument_category, mode_applicable
from lab_control.application.config import config_from_record
from lab_control.application.controls import data_sources, slot_label
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.pages.monitoring.plot import LivePlot


class ResponsiveGrid(QScrollArea):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.grid = QGridLayout(self.body)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(16)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setWidget(self.body)
        self.items: list[QWidget] = []
        self.columns = 0
        self.viewport().installEventFilter(self)

    def eventFilter(self, obj, event):  # noqa: N802
        if obj is self.viewport() and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self.reflow)
        return super().eventFilter(obj, event)

    def set_items(self, items: list[QWidget]) -> None:
        self.items = items
        self.reflow(force=True)

    def reflow(self, force: bool = False) -> None:
        columns = max(1, (self.viewport().width() + 16) // 376)
        if columns == self.columns and not force:
            return
        old_columns = self.columns
        self.columns = columns
        while self.grid.count():
            self.grid.takeAt(0)
        for index in range(max(old_columns, columns)):
            self.grid.setColumnStretch(index, 1 if index < columns else 0)
            self.grid.setColumnMinimumWidth(index, 0)
        for index, item in enumerate(self.items):
            self.grid.addWidget(item, index // columns, index % columns)


class MonitorCard(QFrame):
    action = Signal(str, str)

    def __init__(self, controller: WorkspaceController, spec: dict[str, Any], parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("monitorCard")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setFixedHeight(340 if spec.get("view") == "number" else 370)
        self.controller, self.spec = controller, spec
        layout = QVBoxLayout(self)
        heading = QHBoxLayout()
        self.title = QPushButton(spec["title"])
        self.title.setObjectName("cardLink")
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.title.clicked.connect(lambda: self.action.emit("open", spec["id"]))
        heading.addWidget(self.title, 1)
        for key, label in (("up", "↑"), ("down", "↓"), ("rename", "⋯"), ("remove", "×")):
            button = QPushButton(label)
            button.setMaximumWidth(30)
            button.clicked.connect(lambda _checked=False, action=key: self.action.emit(action, spec["id"]))
            heading.addWidget(button)
        layout.addLayout(heading)
        self.subtitle = QLabel()
        self.subtitle.setWordWrap(True)
        self.subtitle.setObjectName("mutedText")
        layout.addWidget(self.subtitle)
        self.plot = LivePlot()
        layout.addWidget(self.plot, 1)
        self.state = QLabel("未开始监测")
        self.state.setWordWrap(True)
        layout.addWidget(self.state)

    def refresh(self) -> None:
        device_id = self.spec["device_id"]
        record = self.controller.records.get(device_id)
        if not record:
            self.subtitle.setText("设备记录暂不可用 · 保留卡片配置")
            return
        slot = record.get("slots", {}).get(self.controller.mode, "")
        name = record["displayName"] or record["identity"].get("model") or record["adapterId"]
        self.subtitle.setText(
            f"{slot_label(slot)} · {name} · CH {', '.join(map(str, self.spec['channels']))}"
        )
        source = next(
            (s for s in data_sources(config_from_record(record)) if s.key == self.spec["source"]), None
        )
        task = self.controller.runtime.monitoring.snapshots().get(device_id, {})
        if source:
            source = replace(source, view=self.spec.get("view", source.view))
            frame = self.controller.runtime.monitoring.latest(
                device_id, source.kind, source.quantity, self.spec["channels"][0]
            )
            if frame:
                self.plot.show_frame(frame, source, tuple(self.spec["channels"]))
        status = task.get("error") or (
            "监测中 · 记录中"
            if task.get("recording")
            else "监测中 · 未保存数据"
            if task.get("running")
            else "未开始 / 已停止监测"
        )
        self.state.setText(status)


class AddCardDialog(QDialog):
    def __init__(self, controller: WorkspaceController, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("添加监测项目")
        self.resize(520, 330)
        form = QFormLayout(self)
        self.device, self.source, self.view = QComboBox(), QComboBox(), QComboBox()
        self.title = QLineEdit()
        self.checks: dict[int, QCheckBox] = {}
        self.channel_box = QWidget()
        self.channel_layout = QHBoxLayout(self.channel_box)
        form.addRow("设备 / 槽位", self.device)
        form.addRow("曲线或数值项目", self.source)
        form.addRow("通道", self.channel_box)
        form.addRow("显示方式", self.view)
        form.addRow("卡片名称", self.title)
        for record in controller.records.values():
            if record["adapterId"] and mode_applicable(record, controller.mode):
                label = record["displayName"] or record["identity"].get("model") or record["adapterId"]
                slot = record.get("slots", {}).get(controller.mode, "")
                self.device.addItem((slot_label(slot) + " · " if slot else "") + label, record["id"])
        self.device.currentIndexChanged.connect(self.device_changed)
        self.source.currentIndexChanged.connect(self.source_changed)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(self.device.count() > 0)
        form.addRow(buttons)
        self.device_changed()

    def device_changed(self) -> None:
        device_id = self.device.currentData()
        self.source.clear()
        while self.channel_layout.count():
            item = self.channel_layout.takeAt(0)
            widget = item.widget() if item else None
            if widget:
                widget.deleteLater()
        self.checks = {}
        if not device_id:
            return
        record = self.controller.records[device_id]
        for source in data_sources(config_from_record(record)):
            self.source.addItem(source.label, source.key)
        channels = (
            self.controller.capabilities(device_id).channels
            if instrument_category(record) in {"oscilloscope", "power_supply"}
            else (1,)
        )
        for channel in channels:
            check = QCheckBox(f"CH {channel}")
            check.setChecked(channel == channels[0])
            self.channel_layout.addWidget(check)
            self.checks[channel] = check
        self.source_changed()

    def source_changed(self) -> None:
        device_id = self.device.currentData()
        if not device_id:
            return
        source = next(
            (
                s
                for s in data_sources(config_from_record(self.controller.records[device_id]))
                if s.key == self.source.currentData()
            ),
            None,
        )
        self.view.clear()
        if source:
            if (source.kind == "scalar" and not source.quantity) or source.kind == "status":
                self.view.addItem("趋势与数值", "curve")
                self.view.addItem("数值与迷你趋势", "number")
            else:
                self.view.addItem("曲线", source.view)
            self.title.setText(self.source.currentText())


class MonitoringDashboard(QWidget):
    open_card = Signal(object)

    def __init__(self, controller: WorkspaceController, parent=None) -> None:
        super().__init__(parent)
        self.controller, self.mode = controller, ""
        self.specs: list[dict[str, Any]] = []
        self.cards: dict[str, MonitorCard] = {}
        layout = QVBoxLayout(self)
        heading = QLabel("实时监测 / 总览")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        toolbar = QHBoxLayout()
        toolbar.addStretch()
        for text, action in (
            ("添加监测卡片", self.add_dialog),
            ("开始总览监测", self.start),
            ("停止总览监测", self.stop),
        ):
            button = QPushButton(text)
            button.clicked.connect(action)
            toolbar.addWidget(button)
        layout.addLayout(toolbar)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.grid = ResponsiveGrid()
        layout.addWidget(self.grid, 1)
        controller.refreshed.connect(self.refresh)
        self.refresh()

    def persist(self) -> None:
        self.controller.runtime.repository.save_workspace(f"monitor_layout/{self.mode}", self.specs)

    def refresh(self) -> None:
        if self.mode != self.controller.mode:
            self.mode = self.controller.mode
            for card in self.cards.values():
                card.hide()
                card.deleteLater()
            self.cards = {}
            self.specs = self.controller.runtime.repository.load_workspace(f"monitor_layout/{self.mode}", [])
            for spec in self.specs:
                self._create_card(spec)
            self.grid.set_items([self.cards[s["id"]] for s in self.specs])
        if self.isVisible():
            for card in self.cards.values():
                card.refresh()
        self.hint.setText(f"{len(self.specs)} 个监测项目 · 添加卡片不会启动仪器 · 记录需在类型页显式开启")

    def _create_card(self, spec: dict[str, Any]) -> None:
        card = MonitorCard(self.controller, spec)
        card.action.connect(self.card_action)
        self.cards[spec["id"]] = card

    def add(
        self, device_id: str, source: str, channels: tuple[int, ...], title: str = "", view: str = ""
    ) -> None:
        self.refresh()
        record = self.controller.records[device_id]
        data_source = next(s for s in data_sources(config_from_record(record)) if s.key == source)
        if not channels:
            raise ValueError("至少选择一个通道")
        allowed = self.controller.capabilities(device_id).channels
        if any(channel not in allowed for channel in channels):
            raise ValueError(f"通道超出设备范围：{allowed}")
        self.controller.ensure_applicable(device_id)
        spec = {
            "id": uuid4().hex,
            "device_id": device_id,
            "source": source,
            "channels": list(channels),
            "title": title or data_source.label,
            "view": view or data_source.view,
        }
        self.specs.append(spec)
        self._create_card(spec)
        self.grid.set_items([self.cards[s["id"]] for s in self.specs])
        self.persist()
        self.controller.message.emit("已添加监测卡片；尚未启动监测")

    def add_dialog(self) -> None:
        dialog = AddCardDialog(self.controller, self)
        if dialog.exec():
            try:
                self.add(
                    dialog.device.currentData(),
                    dialog.source.currentData(),
                    tuple(c for c, check in dialog.checks.items() if check.isChecked()),
                    dialog.title.text(),
                    dialog.view.currentData(),
                )
            except Exception as exc:
                self.controller.message.emit(str(exc))

    def card_action(self, action: str, card_id: str) -> None:
        index = next(i for i, spec in enumerate(self.specs) if spec["id"] == card_id)
        spec = self.specs[index]
        if action == "open":
            record = self.controller.records.get(spec["device_id"])
            if record:
                self.controller.navigate.emit("实时监测", instrument_category(record), record["id"])
                self.open_card.emit(spec)
            return
        if action == "rename":
            value, accepted = QInputDialog.getText(self, "卡片名称", "名称", text=spec["title"])
            if not accepted or not value.strip():
                return
            spec["title"] = value.strip()
            self.cards[card_id].title.setText(value.strip())
        elif action == "remove":
            card = self.cards.pop(card_id)
            card.hide()
            self.grid.grid.removeWidget(card)
            card.deleteLater()
            self.specs.pop(index)
        else:
            target = index + (-1 if action == "up" else 1)
            if 0 <= target < len(self.specs):
                self.specs[index], self.specs[target] = self.specs[target], self.specs[index]
        self.grid.set_items([self.cards[s["id"]] for s in self.specs])
        self.persist()

    def start(self) -> None:
        grouped: dict[str, list[Any]] = {}
        for spec in self.specs:
            record = self.controller.records.get(spec["device_id"])
            if not record:
                continue
            source = next(
                (s for s in data_sources(config_from_record(record)) if s.key == spec["source"]), None
            )
            if source is None:
                self.controller.message.emit(f"卡片 {spec['title']} 的数据源已不可用，请重新选择")
                continue
            requires_start = record["adapterId"] in {"pico.6404e", "keysight.3458a"}
            grouped.setdefault(record["id"], []).append(
                source.request(tuple(spec["channels"]), initiate=requires_start, confirmed=requires_start)
            )
        dangerous = [
            self.controller.records[key]["displayName"] or self.controller.records[key]["adapterId"]
            for key, requests in grouped.items()
            if any(r.initiate for r in requests)
        ]
        if (
            dangerous
            and QMessageBox.question(
                self,
                "确认监测采集",
                "以下设备将按批次启动采集：\n" + "\n".join(dangerous),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        for device_id, requests in grouped.items():
            try:
                self.controller.ensure_applicable(device_id)
                self.controller.runtime.monitoring.start(
                    device_id, tuple(requests), mode=self.controller.mode
                )
            except Exception as exc:
                self.controller.message.emit(f"{self.controller.records[device_id]['adapterId']}：{exc}")

    def stop(self) -> None:
        for device_id in {spec["device_id"] for spec in self.specs}:
            self.controller.watch(self.controller.runtime.monitoring.stop(device_id))
