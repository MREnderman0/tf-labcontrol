"""Independent K+K FXE compatibility module; import is hardware-free."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import KpluskFxeAdapter, create
from .discovery import normalize_discovery_config
from .profile import capability_schema, profile

DEFINITION = DeviceDefinition(
    profile(),
    KpluskFxeAdapter,
    DeviceDocumentation(
        "K+K FXE",
        "measurement-analyzers/kplusk-fxe",
        "Ethernet（经交换机直连设备 IP）；USB 仅用于官方配置工具",
        "K+K Library 21.2.2 / Python wrapper 2.1",
    ),
    Path(__file__).parent,
    deployment_components=("kplusk-library-win", "kplusk-python-wrapper"),
    factory=create,
    capability_schema=capability_schema,
    identity_requires_adapter=True,
    discovery_config=normalize_discovery_config,
)

__all__ = ["DEFINITION", "KpluskFxeAdapter"]
