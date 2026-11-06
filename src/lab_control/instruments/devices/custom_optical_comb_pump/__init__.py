"""Manual-only connection metadata for a custom RS232 device."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation, DeviceModel

from .adapter import ADAPTER_ID, MODEL, SOURCE, OpticalCombPumpAdapter, capability_schema
from .transport import create_transport


def verify_readonly(*args, **kwargs):
    from .hardware.readonly import verify

    return verify(*args, **kwargs)


DEFINITION = DeviceDefinition(
    None,
    None,
    DeviceDocumentation(
        MODEL,
        "temperature-load-drivers/pump-driver",
        "RS232，手动选择 COM 口，115200 8N1",
        "现有 VISA 运行库与实际串口适配器驱动",
    ),
    Path(__file__).parent,
    deployment_components=("rs-visa", "serial-interface"),
    transport_factory=create_transport,
    transport_adapter=OpticalCombPumpAdapter,
    capability_schema=capability_schema,
    model=DeviceModel(ADAPTER_ID, ("自制",), (MODEL,), "pump_driver", SOURCE, "未注明", "scalar"),
    supported_transports=("visa",),
    acquisition_kinds=("scalar",),
    acquisition_initiate_editable=False,
    manual_connection_only=True,
    readonly_check=verify_readonly,
)
