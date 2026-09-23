from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from lab_control.demo.store import DemoStore
from lab_control.domain.models import DeviceCategory, DeviceSummary


class DeviceDrawer(QFrame):
    closed = Signal()

    def __init__(self, store: DemoStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("deviceDrawer")
        self.setFixedWidth(410)
        self.store = store
        self.category: DeviceCategory | None = None
        self._rows_layout: QVBoxLayout
        self._build()
        self.store.changed.connect(self.refresh)
        self.hide()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)

        header = QHBoxLayout()
        self.title = QLabel("设备详情")
        self.title.setObjectName("drawerTitle")
        close = QPushButton("×")
        close.setObjectName("closeButton")
        close.setFixedSize(32, 32)
        close.clicked.connect(self.closed)
        header.addWidget(self.title)
        header.addStretch(1)
        header.addWidget(close)
        layout.addLayout(header)

        self.description = QLabel("选择设备类别查看演示状态。")
        self.description.setObjectName("mutedText")
        self.description.setWordWrap(True)
        layout.addWidget(self.description)

        self.scan_button = QPushButton("扫描设备（演示）")
        self.scan_button.setObjectName("secondaryButton")
        self.scan_button.clicked.connect(self.store.scan_demo)
        layout.addWidget(self.scan_button)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setObjectName("drawerScroll")
        scroll.viewport().setObjectName("drawerViewport")
        scroll.viewport().setAutoFillBackground(False)
        rows = QWidget()
        rows.setObjectName("drawerBody")
        rows.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._rows_layout = QVBoxLayout(rows)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(10)
        scroll.setWidget(rows)
        layout.addWidget(scroll, 1)

        note = QLabel("演示按钮只改变内存状态，不会连接真实设备。")
        note.setObjectName("drawerNote")
        note.setWordWrap(True)
        layout.addWidget(note)

    def set_category(self, category: DeviceCategory) -> None:
        self.category = category
        self.title.setText(f"{category.label} · 设备详情")
        self.description.setText(category.description)
        self.refresh()
        self.show()

    def refresh(self) -> None:
        if self.category is None:
            return
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            widget = item.widget() if item else None
            if widget:
                widget.deleteLater()
        devices = self.store.devices_for_category(self.category)
        if not devices:
            empty = QLabel("当前模式下没有可见的示例设备。")
            empty.setObjectName("mutedText")
            self._rows_layout.addWidget(empty)
        for device in devices:
            self._rows_layout.addWidget(DeviceRow(device, self.store))
        self._rows_layout.addStretch(1)


class DeviceRow(QFrame):
    def __init__(self, device: DeviceSummary, store: DemoStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("deviceRow")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(5)

        header = QHBoxLayout()
        name = QLabel(f"{device.manufacturer} {device.model}")
        name.setObjectName("rowTitle")
        state = QLabel(device.connection_state.label)
        state.setObjectName(
            "stateConnected" if device.connection_state.value == "connected" else "stateMuted"
        )
        header.addWidget(name)
        header.addStretch(1)
        header.addWidget(state)
        layout.addLayout(header)

        details = QLabel(f"{device.device_id}  ·  {device.address}")
        details.setObjectName("rowDetails")
        details.setWordWrap(True)
        layout.addWidget(details)

        slot = device.slot_name or "请选择设备"
        slot_label = QLabel(f"功能槽位：{slot}")
        slot_label.setObjectName("rowDetails")
        layout.addWidget(slot_label)

        action = QPushButton("断开演示" if device.connection_state.value == "connected" else "连接演示")
        action.setObjectName("smallButton")
        action.clicked.connect(lambda: store.toggle_connection(device.device_id))
        layout.addWidget(action, alignment=Qt.AlignmentFlag.AlignRight)
