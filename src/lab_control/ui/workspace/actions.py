from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QInputDialog,
    QLabel,
    QTextBrowser,
    QVBoxLayout,
)

from lab_control.application.bulk_connection import device_label
from lab_control.application.catalog import instrument_category
from lab_control.application.config import config_from_record
from lab_control.application.controls import CATEGORY_LABELS, slot_label
from lab_control.domain.instruments import jsonable
from lab_control.domain.workspace import category_slots
from lab_control.instruments.registry import describe
from lab_control.ui.widgets.workspace_combo import choose_item

if TYPE_CHECKING:
    from lab_control.ui.real_window import RealWindow


class WorkspaceActions:
    """User-requested device actions and asynchronous result presentation."""

    def __init__(self, window: RealWindow) -> None:
        self.window = window

    def prepare_first_run(self) -> None:
        window = self.window
        from lab_control.ui.pages.component_preparation import show_preparation

        show_preparation(window.runtime, window, first_run=True)
        window.scan()

    def scan(self) -> None:
        window = self.window
        try:
            window.bridge.watch(window.runtime.discovery.scan(), "scan")
            window.refresh()
        except Exception as exc:
            window._failed(str(exc))

    def action(self, key: str) -> None:
        window = self.window
        device_id = window.selected_id
        try:
            if key == "connect":
                config = config_from_record(window.records[device_id])
                if config.usb_instance_id and not config.expected_serial:
                    serial, accepted = QInputDialog.getText(
                        window, "核对设备序列号", "请输入设备铭牌序列号；连接后将与设备实际读回再次核对"
                    )
                    if not accepted or not serial.strip():
                        return
                    from dataclasses import replace

                    window.runtime.add(replace(config, expected_serial=serial.strip()))
                window.bridge.watch(window.runtime.connect(device_id), "connect")
            elif key in {"disconnect", "reconnect"}:

                def change():
                    window.runtime.sessions.disconnect(device_id, drain_reads=True)
                    if key == "reconnect":
                        return window.runtime.connect(device_id).result()

                window.bridge.watch(window.bridge.worker.submit(change), key)
            elif key in {"identify", "status"}:
                window.bridge.watch(window.runtime.sessions.submit(device_id, key), key)
            elif key == "capabilities":
                snapshot = window.runtime.sessions.capability_snapshot(device_id)
                window._completed(
                    "capabilities", snapshot or describe(config_from_record(window.records[device_id]))
                )
            elif key == "stop":
                window.runtime.sessions.stop_acquisition(device_id)
                window.controller.watch(window.runtime.monitoring.stop(device_id))
            elif key in {"manage", "monitor"}:
                category = instrument_category(window.records[device_id])
                if category in CATEGORY_LABELS:
                    window.navigate("设备管理" if key == "manage" else "实时监测", category, device_id)
            elif key == "diagnostic":
                window._completed(
                    "diagnostics",
                    {"device": window.records[device_id], "session": window.states.get(device_id, {})},
                )
            elif key == "resource":
                value, accepted = choose_item(
                    window,
                    "主连接资源",
                    "选择已发现的资源",
                    window.records[device_id]["resources"],
                )
                if accepted:
                    window.runtime.select_resource(device_id, value)
            elif key == "configure":
                window.configure_adapter(device_id)
            elif key == "rename":
                value, accepted = QInputDialog.getText(window, "设备显示名", "名称")
                if accepted:
                    window.runtime.inventory.edit(device_id, display_name=value)
            elif key == "slot":
                window.choose_slot(device_id)
        except Exception as exc:
            window._failed(str(exc))
        window.refresh()

    def choose_slot(self, device_id: str) -> None:
        window = self.window
        mode = window.controller.mode
        records = window.runtime.inventory.all()
        record = next(r for r in records if r["id"] == device_id)
        category = instrument_category(record)
        slots = ["", *category_slots(category)]
        owners = {r.get("slots", {}).get(mode): r for r in records if r.get("slots", {}).get(mode)}
        labels = ["解除绑定"]
        for slot in slots[1:]:
            owner = owners.get(slot)
            suffix = f"（已绑定：{device_label(owner)}）" if owner else "（空闲）"
            labels.append(slot_label(slot) + suffix)
        value, accepted = choose_item(window, "功能槽位", "选择槽位；已占用槽位可确认替换", labels)
        if not accepted:
            return
        slot = slots[labels.index(value)]
        owner = owners.get(slot)
        replace_id = None
        if owner and owner["id"] != device_id:
            dialog = QDialog(window)
            dialog.setWindowTitle("替换槽位绑定")
            dialog.setMinimumWidth(420)
            layout = QVBoxLayout(dialog)
            prompt = QLabel(f"已有【{device_label(owner)}】绑定【{slot_label(slot)}】，是否替换？")
            prompt.setWordWrap(True)
            layout.addWidget(prompt)
            note = QLabel("替换后，原设备保持连接，槽位变为未绑定。")
            note.setWordWrap(True)
            layout.addWidget(note)
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
            )
            confirm = buttons.button(QDialogButtonBox.StandardButton.Ok)
            confirm.setText("替换")
            confirm.setAutoDefault(False)
            cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
            cancel.setText("取消")
            cancel.setDefault(True)
            cancel.setFocus()
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            dialog.deleteLater()
            if not accepted:
                return
            replace_id = owner["id"]
        window.runtime.bind_slot(device_id, slot, mode, replace_device_id=replace_id)

    def connect_all(self) -> None:
        window = self.window
        try:
            window.bridge.watch(window.runtime.connect_all_discovered(), "connect_all")
        except Exception as exc:
            window._failed(str(exc))
        window.refresh()

    def show_connection_results(self) -> None:
        window = self.window
        progress = window.runtime.connection_progress()
        dialog = QDialog(window)
        dialog.setWindowTitle("一键连接结果")
        dialog.resize(680, 440)
        layout = QVBoxLayout(dialog)
        summary = QLabel(window.connection_result_text(progress))
        summary.setWordWrap(True)
        layout.addWidget(summary)
        text = QTextBrowser()
        labels = {"connected": "成功", "already_connected": "已连接", "failed": "失败", "skipped": "跳过"}
        text.setPlainText(
            "\n\n".join(
                f"{item['label']}\n{labels[item['status']]}：{item['reason']}" for item in progress["items"]
            )
        )
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()
        dialog.deleteLater()

    def _completed(self, label: str, result: Any) -> None:
        window = self.window
        window.message.setText(f"{label} 完成")
        if label == "connect_all":
            window.message.setText(window.connection_result_text(result))
        elif label in {"capabilities", "diagnostics"}:
            dialog = QDialog(window)
            dialog.setWindowTitle("能力与诊断")
            dialog.resize(760, 600)
            layout = QVBoxLayout(dialog)
            text = QTextBrowser()
            text.setPlainText(json.dumps(jsonable(result), ensure_ascii=False, indent=2))
            layout.addWidget(text)
            dialog.exec()
        elif label == "scan":
            failures = {name: data for name, data in result["providers"].items() if data["state"] == "error"}
            window.message.setText(
                "扫描完成；" + (json.dumps(failures, ensure_ascii=False) if failures else "已有会话保持不变")
            )
        elif label == "address_scan":
            failures = {name: data for name, data in result["providers"].items() if data["state"] == "error"}
            failed_items = [item for item in result.get("items", ()) if item.get("error")]
            if window._static_address_status is not None:
                if failures or failed_items:
                    detail = {"providers": failures, "items": failed_items}
                    window._static_address_status.setText(
                        "地址扫描完成，但有失败：" + json.dumps(detail, ensure_ascii=False)
                    )
                else:
                    window._static_address_status.setText("地址扫描完成；已更新设备池")
            window.message.setText("地址列表扫描完成")
            if window._refresh_static_address_dialog is not None:
                window._refresh_static_address_dialog()
        window.refresh()

    def _failed(self, message: str) -> None:
        window = self.window
        window.message.setText(message)
        if window._static_address_status is not None:
            window._static_address_status.setText(message)
        if window.runtime.closed:
            window.closing = True
            window.bridge.worker.shutdown(wait=False)
            window.close()

    def diagnose(self) -> None:
        window = self.window
        window.navigate("系统设置")
        window.environment_panel.start()
