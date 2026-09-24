"""SIGLENT SDG2082X independent adapter package."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import Sdg2082xAdapter, sdg2082x

DEFINITION = DeviceDefinition(
    sdg2082x(),
    Sdg2082xAdapter,
    DeviceDocumentation(
        "Siglent SDG2082X",
        "signal-sources/sdg2082x",
        "USB USBTMC / LAN VXI-11，经 PyVISA 接入",
        "64 位 VISA Runtime 和 USBTMC 驱动",
    ),
    Path(__file__).parent,
)
