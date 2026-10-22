"""R&S RTO64: independent protocol and documentation package."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import Rto64Adapter, rto64

DEFINITION = DeviceDefinition(
    rto64(),
    Rto64Adapter,
    DeviceDocumentation("R&S RTO64", "oscilloscopes/rto64", "LAN / USB VISA", "64 位 VISA Runtime"),
    Path(__file__).parent,
    deployment_components=("rs-visa",),
)
