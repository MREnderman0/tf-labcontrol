from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from lab_control.application.catalog import instrument_category
from lab_control.application.controls import CATEGORY_LABELS, slot_label
from lab_control.ui.controller import WorkspaceController, state_label


class DeviceSelector(QWidget):
    selected = Signal(str)
    channel_changed = Signal(int)

    def __init__(self, controller: WorkspaceController, category: str, parent=None) -> None:
        super().__init__(parent)
        self.controller, self.category = controller, category
        self.device_id = ""
        self.signature: tuple[tuple[str, str], ...] | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.combo, self.channel = QComboBox(), QComboBox()
        self.combo.setMinimumWidth(180)
        self.combo.currentIndexChanged.connect(self._selected)
        self.channel.currentIndexChanged.connect(
            lambda: self.channel_changed.emit(self.channel.currentData() or 1)
        )
        row.addWidget(self.combo, 1)
        row.addWidget(QLabel("通道"))
        row.addWidget(self.channel)
        self.connect_button = QPushButton("连接")
        self.connect_button.clicked.connect(self._connect)
        row.addWidget(self.connect_button)
        layout.addLayout(row)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setObjectName("mutedText")
        layout.addWidget(self.info)
        controller.refreshed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        records = [r for r in self.controller.records.values() if instrument_category(r) == self.category]
        entries = []
        slots = {r.get("slots", {}).get(self.controller.mode) for r in records}
        for index in (1, 2):
            slot = f"{self.category}:{index}"
            if slot not in slots:
                entries.append((slot_label(slot) + " · 未绑定", ""))
        for entry in records:
            label = entry["displayName"] or entry["identity"].get("model") or entry["adapterId"]
            slot = entry.get("slots", {}).get(self.controller.mode)
            entries.append(((slot_label(slot) + " · " if slot else "") + label, entry["id"]))
        signature = tuple(entries)
        if signature != self.signature:
            previous = self.device_id
            self.combo.blockSignals(True)
            self.combo.clear()
            for label, device_id in entries:
                self.combo.addItem(label, device_id)
            target = self.combo.findData(previous) if previous else -1
            if target < 0:
                target = next((i for i, (_, key) in enumerate(entries) if key), 0)
            self.combo.setCurrentIndex(target)
            self.combo.blockSignals(False)
            self.signature = signature
            self._selected()
        record = self.controller.records.get(self.device_id)
        state = self.controller.states.get(self.device_id, {})
        self.connect_button.setEnabled(bool(record) and not state)
        self.connect_button.setText(
            state_label(state.get("connectionState", "disconnected")) if state else "连接"
        )
        if record:
            identity = record["identity"]
            self.info.setText(
                f"{identity.get('model') or record['adapterId']}  ·  {record['config']['resource']}\n"
                f"序列号：{identity.get('serial') or '未识别'}  ·  "
                + ("待实机验收" if self.controller.runtime.hardware else "模拟设备")
            )
        else:
            self.info.setText(f"{CATEGORY_LABELS[self.category]} A / B 已预留；先在设备总览连接并绑定设备。")

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

    def _connect(self) -> None:
        try:
            if self.device_id:
                self.controller.watch(self.controller.runtime.connect(self.device_id))
        except Exception as exc:
            self.controller.message.emit(str(exc))
