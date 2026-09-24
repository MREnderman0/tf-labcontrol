"""Tektronix AFG31252 independent adapter package."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import Afg31252Adapter, afg31252

DEFINITION = DeviceDefinition(
    afg31252(),
    Afg31252Adapter,
    DeviceDocumentation(
        "Tektronix AFG31252",
        "signal-sources/afg31252",
        "USB Device / LAN / GPIB（核对实际接口）",
        "可用的 64 位 VISA Runtime 和匹配的接口驱动；优先复用已验证的安装",
    ),
    Path(__file__).parent,
)
