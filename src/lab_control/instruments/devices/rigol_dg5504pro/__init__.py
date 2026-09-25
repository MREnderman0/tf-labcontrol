"""RIGOL DG5504 Pro independent model package."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import Dg5504ProAdapter
from .profile import dg5504pro

DEFINITION = DeviceDefinition(
    dg5504pro(),
    Dg5504ProAdapter,
    DeviceDocumentation(
        "RIGOL DG5504 Pro", "signal-sources/dg5504-pro", "LAN / USB；优先 PyVISA", "64 位 VISA Runtime"
    ),
    Path(__file__).parent,
    deployment_components=("rs-visa",),
)
