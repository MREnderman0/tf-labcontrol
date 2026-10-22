"""R&S ZNB3004: independent protocol and documentation package."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import Znb3004Adapter, network

DEFINITION = DeviceDefinition(
    network(),
    Znb3004Adapter,
    DeviceDocumentation(
        "R&S ZNB3004", "measurement-analyzers/rs-znb3004", "LAN / USB VISA", "64 位 VISA Runtime"
    ),
    Path(__file__).parent,
    deployment_components=("rs-visa",),
)
