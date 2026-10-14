from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from lab_control.application.bulk_connection import device_label
from lab_control.application.controls import CATEGORY_LABELS, slot_label
from lab_control.ui.controller import WorkspaceController, display_connection_state, state_label
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox


class DeviceSelector(QWidget):
    selected = Signal(str)
    channel_changed = Signal(int)

    def __init__(self, controller: WorkspaceController, category: str, parent=None, *, compact=False) -> None:
        super().__init__(parent)
        self.controller, self.category = controller, category
        self.device_id = ""
        self.signature: tuple[tuple[str, str], ...] | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.combo, self.channel = WorkspaceComboBox(), WorkspaceComboBox()
        self.combo.setMinimumWidth(180)
        self.combo.currentIndexChanged.connect(self._selected)
        self.channel.currentIndexChanged.connect(
            lambda: self.channel_changed.emit(self.channel.currentData() or 1)
        )
        row.addWidget(self.combo, 1)
        channel_label = QLabel("通道")
        row.addWidget(channel_label)
        row.addWidget(self.channel)
        self.connect_button = QPushButton("前往设备总览连接")
        self.connect_button.clicked.connect(lambda: controller.navigate.emit("设备管理", "", ""))
        row.addWidget(self.connect_button)
        layout.addLayout(row)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setObjectName("mutedText")
        layout.addWidget(self.info)
        if compact:
            channel_label.hide()
            self.channel.hide()
            self.info.hide()
        controller.refreshed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        records = self.controller.connected_records(self.category)
        entries = []
        for entry in records:
            label = device_label(entry)
            slot = entry.get("slots", {}).get(self.controller.mode)
            connection = display_connection_state(self.controller.states[entry["id"]])
            suffix = " · " + state_label(connection) if connection != "connected" else ""
            entries.append(((slot_label(slot) + " · " if slot else "") + label + suffix, entry["id"]))
        signature = tuple(entries)
        if signature != self.signature:
            self.combo.replace_items(entries, select_first=self.signature is None)
            self.signature = signature
        self.combo.setPlaceholderText("选择已连接设备" if entries else "暂无已连接设备")
        record = self.controller.records.get(self.device_id)
        self.connect_button.setVisible(not bool(self.device_id))
        self.channel.setEnabled(bool(self.device_id))
        if record:
            identity = record["identity"]
            serial = identity.get("serial") or record["config"].get("expected_serial") or "未识别"
            serial_label = "序列号" if identity.get("serial") else "资产序列号（配置）" if record["config"].get("expected_serial") else "序列号"
            self.info.setText(
                f"{identity.get('model') or record['adapterId']}  ·  {record['config']['resource']}\n"
                f"{serial_label}：{serial}  ·  "
                + ("待实机验收" if self.controller.runtime.hardware else "模拟设备")
            )
        else:
            self.info.setText(f"请先在设备总览连接{CATEGORY_LABELS[self.category]}，再选择设备。")

    def _selected(self) -> None:
        device_id = self.combo.currentData() or ""
        if device_id == self.device_id:
            return
        self.device_id = device_id
        self.channel.blockSignals(True)
        self.channel.clear()
        if device_id:
            for channel in self.controller.capabilities(device_id).channels:
                self.channel.addItem(f"CH {channel}", channel)
        self.channel.blockSignals(False)
        self.selected.emit(device_id)

    def select(self, device_id: str, channel: int = 1) -> None:
        self.refresh()
        index = self.combo.findData(device_id)
        if index >= 0:
            self.combo.setCurrentIndex(index)
            index = self.channel.findData(channel)
            self.channel.setCurrentIndex(max(0, index))
        else:
            # A stale navigation target must never leave a different device selected.
            self.combo.setCurrentIndex(-1)
            self._selected()
        self.refresh()
