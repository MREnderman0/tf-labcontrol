"""Independent Joinwit JW8105 protocol package."""

from pathlib import Path

from lab_control.domain.processing import ProcessingProfile
from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import Jw8105Adapter
from .profile import capability_schema, profile
from .transport import create_transport

DEFINITION = DeviceDefinition(
    profile(),
    Jw8105Adapter,
    DeviceDocumentation(
        "Joinwit JW8105",
        "measurement-analyzers/joinwit-jw8105",
        "RS232；RJ45 选件待核对",
        "V23.02.02 二进制协议",
    ),
    Path(__file__).parent,
    deployment_components=("serial-interface",),
    transport_factory=create_transport,
    capability_schema=capability_schema,
    identity_requires_adapter=True,
    offline_processing=ProcessingProfile("scalar", power_units=True),
)

__all__ = ["DEFINITION", "Jw8105Adapter"]
