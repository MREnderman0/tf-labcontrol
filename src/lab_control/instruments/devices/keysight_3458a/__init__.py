"""Keysight / Agilent / HP 3458A: independent protocol and documentation package."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import Keysight3458aAdapter, multimeter
from .transport import create_transport

DEFINITION = DeviceDefinition(
    multimeter(),
    Keysight3458aAdapter,
    DeviceDocumentation(
        "Keysight / Agilent / HP 3458A",
        "measurement-analyzers/keysight-3458a",
        "GPIB + USB-GPIB/LAN-GPIB 控制器",
        "控制器驱动和 64 位 VISA Runtime",
    ),
    Path(__file__).parent,
    deployment_components=("rs-visa", "gpib-interface"),
    transport_factory=create_transport,
    acquisition_initiate_default=True,
    acquisition_requires_confirmation=True,
)
