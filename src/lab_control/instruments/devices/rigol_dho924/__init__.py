"""Importing the DHO924 definition never opens hardware."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import DHO924Adapter
from .profile import profile

DEFINITION = DeviceDefinition(
    profile(),
    DHO924Adapter,
    DeviceDocumentation(
        "RIGOL DHO924", "oscilloscopes/rigol-dho924", "LAN PyVISA / VISA SOCKET", "64 位 VISA Runtime"
    ),
    Path(__file__).parent,
    deployment_components=("rs-visa",),
)
