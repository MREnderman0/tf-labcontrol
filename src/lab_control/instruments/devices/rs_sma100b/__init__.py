"""Rohde & Schwarz SMA100B independent adapter package."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import Sma100bAdapter, sma100b

DEFINITION = DeviceDefinition(
    sma100b(),
    Sma100bAdapter,
    DeviceDocumentation(
        "R&S SMA100B",
        "signal-sources/sma100b",
        "LAN / USB / 已安装 GPIB；优先 PyVISA",
        "64 位 VISA Runtime",
    ),
    Path(__file__).parent,
)
