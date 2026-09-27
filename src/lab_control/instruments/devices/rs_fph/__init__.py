"""Independent R&S Spectrum Rider FPH02 package."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import FphAdapter
from .profile import fph

DEFINITION = DeviceDefinition(
    fph(),
    FphAdapter,
    DeviceDocumentation("R&S FPH02", "spectrum-analyzers/rs-fph", "LAN；优先 PyVISA", "64 位 VISA Runtime"),
    Path(__file__).parent,
    deployment_components=("rs-visa",),
)
