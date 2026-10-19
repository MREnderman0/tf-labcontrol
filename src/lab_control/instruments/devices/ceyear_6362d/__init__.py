"""Ceyear 6362D independent optical spectrum analyzer module."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import Ceyear6362DAdapter
from .authentication import discovery_identity
from .discovery import normalize_discovery_config
from .profile import profile

DEFINITION = DeviceDefinition(
    profile(),
    Ceyear6362DAdapter,
    DeviceDocumentation(
        "Ceyear 6362D", "measurement-analyzers/ceyear-6362d", "LAN Socket 10001；PyVISA", "64 位 VISA Runtime"
    ),
    Path(__file__).parent,
    socket_port=10001,
    deployment_components=("rs-visa",),
    identity_requires_adapter=True,
    discovery_identity=discovery_identity,
    discovery_config=normalize_discovery_config,
)
