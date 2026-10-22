"""Siglent SPD4306X: independent protocol and documentation package."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import Spd4306xAdapter, spd

DEFINITION = DeviceDefinition(
    spd(),
    Spd4306xAdapter,
    DeviceDocumentation(
        "Siglent SPD4306X",
        "power-supplies/spd4306x",
        "USB / LAN VISA / 已配置 TCP Socket",
        "64 位 VISA Runtime 或直接 TCP",
    ),
    Path(__file__).parent,
    deployment_components=("rs-visa",),
)
