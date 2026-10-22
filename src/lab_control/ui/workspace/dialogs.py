from __future__ import annotations

import re
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.config import ConnectionConfig, config_from_record
from lab_control.domain.instruments import DeviceError
from lab_control.instruments.registry import DEVICE_DEFINITIONS, PROFILES, SDK_ADAPTERS
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox

if TYPE_CHECKING:
    from lab_control.ui.real_window import RealWindow


class ConnectionDialogs:
    """Explicit connection configuration dialogs."""

    def __init__(self, window: RealWindow) -> None:
        self.window = window

    def show_static_addresses(self) -> None:
        """Open the compact model-and-endpoint dialog."""
        window = self.window
        if window._static_address_dialog is not None:
            window._static_address_dialog.raise_()
            window._static_address_dialog.activateWindow()
            return

        dialog = QDialog(window)
        dialog.setObjectName("staticAddressDialog")
        dialog.setWindowTitle("添加设备与地址列表")
        dialog.resize(760, 560)
        window._static_address_dialog = dialog
        root = QVBoxLayout(dialog)

        title = QLabel("添加设备")
        title.setObjectName("panelTitle")
        root.addWidget(title)
        description = QLabel("只需要选择型号并填写 IP 或 COM 口。端口、超时和备注已放入高级选项。")
        description.setObjectName("pageDescription")
        description.setWordWrap(True)
        root.addWidget(description)

        form = QFormLayout()
        model = WorkspaceComboBox()
        model.addItem("自动识别（只读探测）", "")
        for adapter_id, profile in sorted(PROFILES.items()):
            manufacturer = profile.manufacturer[0] if profile.manufacturer else "未知厂家"
            models = "/".join(profile.models[:2]) or adapter_id
            model.addItem(f"{manufacturer} {models} · {adapter_id}", adapter_id)
        default_model = model.findData("ceyear.6362d")
        if default_model >= 0:
            model.setCurrentIndex(default_model)
        endpoint = QLineEdit()
        endpoint.setObjectName("staticAddressInput")
        endpoint.setPlaceholderText("例如 192.168.5.241 或 COM3")
        form.addRow("设备型号", model)
        form.addRow("IP / COM 口", endpoint)
        root.addLayout(form)

        advanced_toggle = QCheckBox("显示高级连接选项")
        root.addWidget(advanced_toggle)
        advanced = QWidget()
        advanced_form = QFormLayout(advanced)
        port = QSpinBox()
        port.setRange(1, 65535)
        port.setValue(10001)
        baud = QSpinBox()
        baud.setRange(300, 3_000_000)
        baud.setValue(9600)
        timeout = QDoubleSpinBox()
        timeout.setRange(0.1, 60)
        timeout.setValue(3)
        label = QLineEdit()
        label.setPlaceholderText("可选，例如实验室光谱仪")
        source_address = QLineEdit()
        source_address.setObjectName("staticSourceAddressInput")
        source_address.setPlaceholderText("可选，填写实验网卡 IPv4；6362D 留空自动选择物理网卡")
        advanced_form.addRow("Socket 端口（IP）", port)
        advanced_form.addRow("串口波特率（COM）", baud)
        advanced_form.addRow("通信超时（秒）", timeout)
        advanced_form.addRow("备注（可选）", label)
        advanced_form.addRow("本地网卡地址（可选）", source_address)
        advanced.setVisible(False)
        advanced_toggle.toggled.connect(advanced.setVisible)
        root.addWidget(advanced)

        def update_model_defaults() -> None:
            definition = DEVICE_DEFINITIONS.get(str(model.currentData() or ""))
            if definition and definition.socket_port:
                port.setValue(definition.socket_port)

        model.currentIndexChanged.connect(update_model_defaults)
        update_model_defaults()

        address_count = QLabel()
        address_count.setObjectName("overviewNote")
        root.addWidget(address_count)
        address_list = QListWidget()
        address_list.setObjectName("staticAddressList")
        address_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        root.addWidget(address_list, 1)

        actions = QHBoxLayout()
        save_scan = QPushButton("保存并扫描")
        save_scan.setObjectName("primaryButton")
        remove = QPushButton("删除选中")
        scan_selected = QPushButton("扫描选中")
        scan_all = QPushButton("扫描全部")
        actions.addWidget(save_scan)
        actions.addWidget(remove)
        actions.addWidget(scan_selected)
        actions.addWidget(scan_all)
        actions.addStretch()
        root.addLayout(actions)
        status = QLabel()
        status.setObjectName("overviewNote")
        status.setWordWrap(True)
        root.addWidget(status)

        def refresh_addresses() -> None:
            entries = window.runtime.static_addresses.entries()
            selected = {str(item.data(Qt.ItemDataRole.UserRole)) for item in address_list.selectedItems()}
            address_list.blockSignals(True)
            address_list.clear()
            for entry in entries:
                kind = "COM" if entry.kind == "serial" else "IP"
                model_name = entry.adapter or "自动识别"
                suffix = f" · {entry.label}" if entry.label else ""
                source_suffix = f" · 源 {entry.source_address}" if entry.source_address else ""
                item = QListWidgetItem(f"{kind} {entry.address} · {model_name}{suffix}{source_suffix}")
                item.setData(Qt.ItemDataRole.UserRole, entry.id)
                item.setSelected(entry.id in selected)
                address_list.addItem(item)
            address_list.blockSignals(False)
            address_count.setText(f"已保存 {len(entries)} 个设备地址")
            has_selection = bool(address_list.selectedItems())
            has_items = bool(entries)
            busy = window.runtime.discovery.is_scanning
            remove.setEnabled(has_selection)
            scan_selected.setEnabled(has_selection and not busy)
            scan_all.setEnabled(has_items and not busy)
            save_scan.setEnabled(not busy)

        window._refresh_static_address_dialog = refresh_addresses

        def selected_ids() -> tuple[str, ...]:
            return tuple(str(item.data(Qt.ItemDataRole.UserRole)) for item in address_list.selectedItems())

        def scan_addresses(ids: tuple[str, ...]) -> None:
            if not ids:
                status.setText("请先选择至少一个设备地址。")
                return
            try:
                window.bridge.watch(window.runtime.scan_static_addresses(ids), "address_scan")
                status.setText("正在扫描所选设备地址…")
                refresh_addresses()
            except (DeviceError, ValueError) as exc:
                status.setText(str(exc))

        def save_endpoint() -> None:
            value = endpoint.text().strip()
            kind = "serial" if re.fullmatch(r"(?:COM|ASRL)\s*\d+(?:::INSTR)?", value, re.I) else "ip"
            try:
                entry = window.runtime.static_addresses.add(
                    value,
                    kind=kind,
                    port=port.value(),
                    adapter=str(model.currentData() or ""),
                    timeout=timeout.value(),
                    label=label.text(),
                    baud_rate=baud.value(),
                    source_address=source_address.text(),
                )
                endpoint.clear()
                label.clear()
                source_address.clear()
                refresh_addresses()
                if window.runtime.hardware:
                    scan_addresses((entry.id,))
                else:
                    status.setText("已保存；模拟模式不会访问真实设备。")
            except (DeviceError, ValueError) as exc:
                status.setText(str(exc))

        save_scan.clicked.connect(save_endpoint)

        def remove_selected() -> None:
            window.runtime.static_addresses.remove(set(selected_ids()))
            refresh_addresses()

        remove.clicked.connect(remove_selected)
        scan_selected.clicked.connect(lambda: scan_addresses(selected_ids()))
        scan_all.clicked.connect(
            lambda: scan_addresses(tuple(entry.id for entry in window.runtime.static_addresses.entries()))
        )
        address_list.itemSelectionChanged.connect(refresh_addresses)
        refresh_addresses()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(dialog.reject)
        root.addWidget(buttons)
        window._static_address_status = status

        def clear_dialog() -> None:
            if window._static_address_dialog is dialog:
                window._static_address_dialog = None
                window._static_address_status = None
                window._refresh_static_address_dialog = None

        dialog.finished.connect(lambda _result: clear_dialog())
        dialog.exec()
        dialog.deleteLater()

    def configure_adapter(self, device_id: str) -> None:
        """Let the user bind a known protocol to one enumerated resource."""
        window = self.window
        record = window.records[device_id]
        current = config_from_record(record)
        dialog = QDialog(window)
        dialog.setWindowTitle("为已发现资源指定适配器")
        form = QFormLayout(dialog)
        resource = QLineEdit(current.resource)
        serial = QLineEdit(current.expected_serial)
        baud, timeout = QSpinBox(), QDoubleSpinBox()
        baud.setRange(300, 3000000)
        baud.setValue(current.baud_rate)
        timeout.setRange(0.1, 60)
        timeout.setValue(current.timeout)
        adapter, transport = WorkspaceComboBox(), WorkspaceComboBox()
        adapter.addItems([*PROFILES, *SDK_ADAPTERS])
        if current.adapter:
            adapter.setCurrentText(current.adapter)
        transport.addItems(["visa", "serial", "tcp", "vicp", "tlpmx", "pm100d", "pico", "tserve", "kplusk"])
        transport.setCurrentText(current.transport)
        resource.setReadOnly(False)
        form.addRow("资源地址", resource)
        form.addRow("准确型号适配器", adapter)
        form.addRow("传输方式", transport)
        form.addRow("预期序列号（可选）", serial)
        form.addRow("串口波特率", baud)
        form.addRow("通信超时（秒）", timeout)
        note = QLabel("COM 号只代表本次插入地址；确认适配器后，匹配的 USB 转串口设备会随扫描自动核验。")
        note.setWordWrap(True)
        form.addRow(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec():
            try:
                config = ConnectionConfig(
                    resource.text().strip(),
                    adapter.currentText(),
                    transport.currentText(),
                    expected_serial=serial.text().strip(),
                    baud_rate=baud.value(),
                    timeout=timeout.value(),
                    identity_confirmed=False,
                )
                window.runtime.configure_device(device_id, config)
                window.refresh()
            except (DeviceError, ValueError) as exc:
                window._failed(str(exc))
