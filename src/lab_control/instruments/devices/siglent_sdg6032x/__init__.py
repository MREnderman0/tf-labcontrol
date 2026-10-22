"""Siglent SDG6032X: independent protocol and documentation package."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import Sdg6032xAdapter, sdg6032x

DEFINITION = DeviceDefinition(
    sdg6032x(),
    Sdg6032xAdapter,
    DeviceDocumentation(
        "Siglent SDG6032X",
        "signal-sources/sdg6032x",
        "USB / LAN VXI-11 / 已配置 TCP Socket",
        "VISA Runtime；直接 TCP 不需要 VISA",
    ),
    Path(__file__).parent,
    deployment_components=("rs-visa",),
)
