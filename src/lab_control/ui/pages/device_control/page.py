from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from lab_control.application.controls import CATEGORY_LABELS, MEASUREMENT_CATEGORIES
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.pages.device_control.form import ControlForm
from lab_control.ui.widgets.device_selector import DeviceSelector


class DeviceControlPage(QWidget):
    def __init__(self, controller: WorkspaceController, category: str, parent=None) -> None:
        super().__init__(parent)
        self.controller, self.category = controller, category
        layout = QVBoxLayout(self)
        heading = QLabel(CATEGORY_LABELS[category] + "管理")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        self.selector = DeviceSelector(controller, category, surface="management")
        layout.addWidget(self.selector)
        self.form = ControlForm(controller)
        self.note = QLabel()
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.go_monitor = QPushButton("进入实时监测与参数调节 →")
        self.go_monitor.clicked.connect(
            lambda: controller.navigate.emit("实时监测", category, self.selector.device_id)
        )
        layout.addWidget(self.go_monitor)
        layout.addWidget(self.form, 1)
        self.selector.selected.connect(self.select_device)
        self.selector.channel_changed.connect(lambda channel: self.select_device(self.selector.device_id))
        controller.refreshed.connect(self.refresh)
        self.select_device(self.selector.device_id)

    def select_device(self, device_id: str) -> None:
        measurement = self.category in MEASUREMENT_CATEGORIES
        self.form.setVisible(not measurement and bool(device_id))
        self.go_monitor.setVisible(bool(device_id))
        self.go_monitor.setEnabled(bool(device_id))
        if not measurement:
            self.form.set_device(device_id, self.selector.channel.currentData() or 1)
        if device_id:
            caps = self.controller.capabilities(device_id)
            text = f"已注册 {len(caps.operations)} 项能力 · {len(caps.channels)} 个通道。"
            if measurement:
                text += "影响曲线和测量结果的参数统一放在实时监测页。"
            if self.controller.records[device_id]["adapterId"].startswith("sim."):
                text += " 此类别仅为演示，真实型号和协议待补。"
            self.note.setText(text)
        else:
            self.note.setText("暂无选中的已连接设备。请先在设备总览连接设备。")

    def refresh(self) -> None:
        if self.isVisible() and hasattr(self.form, "mode"):
            if self.form.mode != self.controller.mode:
                self.select_device(self.selector.device_id)
