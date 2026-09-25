"""RIGOL DSA815 independent model package."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import Dsa815Adapter
from .profile import dsa815

DEFINITION = DeviceDefinition(
    dsa815(),
    Dsa815Adapter,
    DeviceDocumentation(
        "RIGOL DSA815", "spectrum-analyzers/rigol-dsa815", "LAN / USB；优先 PyVISA", "64 位 VISA Runtime"
    ),
    Path(__file__).parent,
    deployment_components=("rs-visa",),
    socket_port=5555,
)
