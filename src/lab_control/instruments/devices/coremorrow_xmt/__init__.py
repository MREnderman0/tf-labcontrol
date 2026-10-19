"""Independent CoreMorrow XMT serial protocol package."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import CoreMorrowXmtAdapter
from .discovery import SerialDiscoveryProvider
from .profile import capability_schema, profile
from .transport import create_transport

DEFINITION = DeviceDefinition(
    profile(),
    CoreMorrowXmtAdapter,
    DeviceDocumentation(
        "芯明天 XMT（完整型号待核）",
        "power-supplies/coremorrow-xmt",
        "USB 串口 / VISA ASRL；COM 参数由现场配置指定",
        "CoreMorrow Control Software 25.09.26（仅作为现场证据，未作为运行时 DLL 依赖）",
    ),
    Path(__file__).parent,
    deployment_components=("serial-interface",),
    transport_factory=create_transport,
    capability_schema=capability_schema,
    discovery_factory=SerialDiscoveryProvider,
    identity_requires_adapter=True,
    management_surface=True,
    monitoring_surface=False,
)

__all__ = ["DEFINITION", "CoreMorrowXmtAdapter", "SerialDiscoveryProvider"]
