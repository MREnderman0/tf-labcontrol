"""GW Instek GPD-4303S: four-channel supply with one master output."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import Gpd4303sAdapter, gpd4303s

DEFINITION = DeviceDefinition(
    gpd4303s(),
    Gpd4303sAdapter,
    DeviceDocumentation(
        "GW Instek GPD-4303S",
        "power-supplies/gpd-4303s",
        "USB 虚拟串口；优先 PyVISA ASRL，9600 baud / 8N1 / LF",
        "64 位 VISA Runtime 与 FTDI USB 驱动",
    ),
    Path(__file__).parent,
    deployment_components=("rs-visa", "ftdi-vcp"),
    serial_driver_managed=True,
)
