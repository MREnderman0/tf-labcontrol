"""Independent Microchip 53100A module; import is hardware-free."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import Microchip53100AAdapter, create
from .discovery import UsbDiscoveryProvider
from .profile import capability_schema, profile

DEFINITION = DeviceDefinition(
    profile(),
    Microchip53100AAdapter,
    DeviceDocumentation(
        "Microchip 53100A",
        "measurement-analyzers/phase-noise-53100a",
        "USB：TimeLab 驱动与项目托管 TSERVE64；也支持已有外部 TSERVE",
        "TimeLab 1.780A x64 / 53100A USB 驱动；不使用 VISA/SCPI",
    ),
    Path(__file__).parent,
    deployment_components=("tserve",),
    factory=create,
    capability_schema=capability_schema,
    discovery_factory=UsbDiscoveryProvider,
)
