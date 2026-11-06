"""SIGLENT SPS5000X family protocol and documentation."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import Sps5000xAdapter, sps5000x

DEFINITION = DeviceDefinition(
    sps5000x(),
    Sps5000xAdapter,
    DeviceDocumentation(
        "SIGLENT SPS5000X 系列",
        "power-supplies/sps5000x",
        "USB-TMC / LAN VXI-11 / TCP Socket 5025",
        "64 位 VISA Runtime（USB / VXI-11）",
    ),
    Path(__file__).parent,
    socket_port=5025,
    deployment_components=("rs-visa",),
    supported_transports=("visa", "tcp"),
)
