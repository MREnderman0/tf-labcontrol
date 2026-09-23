from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any


class LabMode(str, Enum):
    ELECTRONIC = "electronic"
    OPTICAL = "optical"

    @property
    def label(self) -> str:
        return "电子学模式" if self is LabMode.ELECTRONIC else "光学模式"


class DeviceCategory(str, Enum):
    SIGNAL_GENERATOR = "signal_generator"
    POWER_SUPPLY = "power_supply"
    OSCILLOSCOPE = "oscilloscope"
    SPECTRUM_ANALYZER = "spectrum_analyzer"
    TEMPERATURE_CONTROLLER = "temperature_controller"
    OPTICAL_POWER_METER = "optical_power_meter"

    @property
    def label(self) -> str:
        return {
            DeviceCategory.SIGNAL_GENERATOR: "信号源",
            DeviceCategory.POWER_SUPPLY: "电源",
            DeviceCategory.OSCILLOSCOPE: "示波器",
            DeviceCategory.SPECTRUM_ANALYZER: "频谱仪",
            DeviceCategory.TEMPERATURE_CONTROLLER: "温控设备",
            DeviceCategory.OPTICAL_POWER_METER: "光学测量",
        }[self]

    @property
    def description(self) -> str:
        return {
            DeviceCategory.SIGNAL_GENERATOR: "频率、幅度和输出状态",
            DeviceCategory.POWER_SUPPLY: "电压、电流与通道状态",
            DeviceCategory.OSCILLOSCOPE: "波形、触发和采样配置",
            DeviceCategory.SPECTRUM_ANALYZER: "频率范围、RBW 与轨迹",
            DeviceCategory.TEMPERATURE_CONTROLLER: "温度读回与稳定状态",
            DeviceCategory.OPTICAL_POWER_METER: "光功率、波长与传感器",
        }[self]


class ConnectionState(str, Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ERROR = "error"

    @property
    def label(self) -> str:
        return {
            ConnectionState.CONNECTED: "已连接",
            ConnectionState.DISCONNECTED: "未连接",
            ConnectionState.ERROR: "异常",
        }[self]


@dataclass(frozen=True)
class DeviceSummary:
    device_id: str
    category: DeviceCategory
    manufacturer: str
    model: str
    address: str
    serial_number: str
    supported_modes: tuple[LabMode, ...]
    connection_state: ConnectionState
    slot_name: str | None = None
    last_updated: str = "刚刚"

    @property
    def supports_current_mode(self) -> bool:
        return True


@dataclass(frozen=True)
class CategoryStats:
    category: DeviceCategory
    discovered: int
    connected: int
    disconnected: int
    errors: int
    bound: int
    visible: bool = True


@dataclass(frozen=True)
class DemoSnapshot:
    mode: LabMode
    devices: tuple[DeviceSummary, ...]
    last_scan_at: str
    status_message: str


def update_device(device: DeviceSummary, **changes: Any) -> DeviceSummary:
    return replace(device, **changes)


def now_label() -> str:
    return datetime.now().strftime("%H:%M:%S")
