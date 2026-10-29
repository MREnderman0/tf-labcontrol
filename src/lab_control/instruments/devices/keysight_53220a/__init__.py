"""Independent Keysight 53220A frequency counter; imports perform no I/O."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import Keysight53220aAdapter
from .profile import profile


def _readonly_check(*args, **kwargs):
    from .hardware.verify import verify_readonly

    return verify_readonly(*args, **kwargs)


DEFINITION = DeviceDefinition(
    profile(),
    Keysight53220aAdapter,
    DeviceDocumentation(
        "Keysight / Agilent 53220A",
        "measurement-analyzers/keysight-53220a",
        "LAN VXI-11 / VISA SOCKET；优先 PyVISA",
        "64 位 VISA Runtime",
    ),
    Path(__file__).parent,
    socket_port=5025,
    deployment_components=("rs-visa",),
    supported_transports=("visa",),
    readonly_check=_readonly_check,
    acquisition_initiate_default=True,
    acquisition_channel_selection="single",
)
