"""Exact SDG6032X-E registration; no dependency on other model packages."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import Sdg6032xeAdapter, sdg6032xe

DEFINITION = DeviceDefinition(
    sdg6032xe(),
    Sdg6032xeAdapter,
    DeviceDocumentation(
        "Siglent SDG6032X-E",
        "signal-sources/sdg6032xe",
        "USB / LAN VXI-11 / explicitly configured VISA SOCKET",
        "PyVISA + Windows x64 VISA Runtime",
    ),
    Path(__file__).parent,
    deployment_components=("rs-visa",),
)
