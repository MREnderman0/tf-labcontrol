"""Independent RIGOL M300 data acquisition and switch-system package."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import M300Adapter
from .profile import m300


def _readonly_check(*args, **kwargs):
    from .hardware.verify import verify_readonly

    return verify_readonly(*args, **kwargs)


DEFINITION = DeviceDefinition(
    m300(),
    M300Adapter,
    DeviceDocumentation(
        "RIGOL M300",
        "measurement-analyzers/rigol-m300",
        "LAN / USB VISA；优先 PyVISA",
        "64 位 VISA Runtime",
    ),
    Path(__file__).parent,
    deployment_components=("rs-visa",),
    monitoring_only=True,
    acquisition_requires_confirmation=True,
    acquisition_initiate_editable=False,
    readonly_check=_readonly_check,
)
