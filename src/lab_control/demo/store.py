from __future__ import annotations

from collections import Counter

from PySide6.QtCore import QObject, Signal

from lab_control.domain.models import (
    CategoryStats,
    ConnectionState,
    DemoSnapshot,
    DeviceCategory,
    DeviceSummary,
    LabMode,
    now_label,
    update_device,
)


class DemoStore(QObject):
    """Small in-memory state store used only by the UI design shell."""

    changed = Signal()
    status_changed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._mode = LabMode.ELECTRONIC
        self._last_scan_at = now_label()
        self._status_message = "设计演示已就绪 · 未连接真实设备"
        self._devices = list(self._seed_devices())

    @staticmethod
    def _seed_devices() -> tuple[DeviceSummary, ...]:
        electronic = (LabMode.ELECTRONIC,)
        optical = (LabMode.OPTICAL,)
        both = (LabMode.ELECTRONIC, LabMode.OPTICAL)
        return (
            DeviceSummary(
                "SG-01",
                DeviceCategory.SIGNAL_GENERATOR,
                "RIGOL",
                "DG5504 Pro",
                "TCPIP0::192.168.1.41::inst0::INSTR",
                "DG5A-DEMO-01",
                both,
                ConnectionState.CONNECTED,
                "移频信号源",
            ),
            DeviceSummary(
                "SG-02",
                DeviceCategory.SIGNAL_GENERATOR,
                "RIGOL",
                "DG5504 Pro",
                "USB0::0x1AB1::0x0641::SG-DEMO-02::INSTR",
                "DG5A-DEMO-02",
                both,
                ConnectionState.DISCONNECTED,
            ),
            DeviceSummary(
                "PSU-01",
                DeviceCategory.POWER_SUPPLY,
                "Keithley",
                "2231A-30-3",
                "ASRL3::INSTR",
                "2231A-DEMO-01",
                electronic,
                ConnectionState.CONNECTED,
                "偏置电源",
            ),
            DeviceSummary(
                "PSU-02",
                DeviceCategory.POWER_SUPPLY,
                "RIGOL",
                "DP832",
                "USB0::0x1AB1::0x0E11::PSU-DEMO-02::INSTR",
                "DP832-DEMO-02",
                electronic,
                ConnectionState.DISCONNECTED,
            ),
            DeviceSummary(
                "OSC-01",
                DeviceCategory.OSCILLOSCOPE,
                "R&S",
                "RTM3004",
                "TCPIP0::192.168.1.43::inst0::INSTR",
                "RTM3-DEMO-01",
                both,
                ConnectionState.CONNECTED,
                "波形采集",
            ),
            DeviceSummary(
                "SA-01",
                DeviceCategory.SPECTRUM_ANALYZER,
                "RIGOL",
                "DSA815",
                "TCPIP0::192.168.1.44::inst0::INSTR",
                "DSA815-DEMO-01",
                electronic,
                ConnectionState.CONNECTED,
            ),
            DeviceSummary(
                "TEMP-01",
                DeviceCategory.TEMPERATURE_CONTROLLER,
                "DemoLab",
                "TC-200",
                "COM5",
                "TC-DEMO-01",
                both,
                ConnectionState.DISCONNECTED,
                "温控通道",
            ),
            DeviceSummary(
                "PM-01",
                DeviceCategory.OPTICAL_POWER_METER,
                "Thorlabs",
                "PM100D + S154C",
                "USB0::0x1313::0x8078::PM-DEMO-01::INSTR",
                "PM100D-DEMO-01",
                optical,
                ConnectionState.CONNECTED,
                "光功率",
            ),
        )

    @property
    def mode(self) -> LabMode:
        return self._mode

    def snapshot(self) -> DemoSnapshot:
        return DemoSnapshot(
            self._mode,
            tuple(self._devices),
            self._last_scan_at,
            self._status_message,
        )

    def visible_devices(self) -> tuple[DeviceSummary, ...]:
        return tuple(device for device in self._devices if self._mode in device.supported_modes)

    def devices_for_category(self, category: DeviceCategory) -> tuple[DeviceSummary, ...]:
        return tuple(device for device in self.visible_devices() if device.category is category)

    def category_stats(self, category: DeviceCategory) -> CategoryStats:
        devices = self.devices_for_category(category)
        counts = Counter(device.connection_state for device in devices)
        return CategoryStats(
            category=category,
            discovered=len(devices),
            connected=counts[ConnectionState.CONNECTED],
            disconnected=counts[ConnectionState.DISCONNECTED],
            errors=counts[ConnectionState.ERROR],
            bound=sum(device.slot_name is not None for device in devices),
            visible=bool(devices),
        )

    def set_mode(self, mode: LabMode) -> None:
        if mode is self._mode:
            return
        self._mode = mode
        self._set_status(f"已切换到{mode.label} · 仅更新设计演示状态")
        self.changed.emit()

    def toggle_connection(self, device_id: str) -> None:
        for index, device in enumerate(self._devices):
            if device.device_id != device_id:
                continue
            new_state = (
                ConnectionState.DISCONNECTED
                if device.connection_state is ConnectionState.CONNECTED
                else ConnectionState.CONNECTED
            )
            self._devices[index] = update_device(device, connection_state=new_state, last_updated="刚刚")
            self._set_status(f"{device.model} 已演示{new_state.label} · 不会连接真实设备")
            self.changed.emit()
            return

    def scan_demo(self) -> None:
        self._last_scan_at = now_label()
        self._set_status(f"演示扫描完成 · {len(self._devices)} 台示例设备")
        self.changed.emit()

    def _set_status(self, message: str) -> None:
        self._status_message = message
        self.status_changed.emit(message)
