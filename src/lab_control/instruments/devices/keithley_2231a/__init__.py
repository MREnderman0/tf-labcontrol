"""Keithley 2231A-30-3: independent protocol and documentation package."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import Keithley2231aAdapter, keithley

DEFINITION = DeviceDefinition(
    keithley(),
    Keithley2231aAdapter,
    DeviceDocumentation(
        "Keithley 2231A-30-3",
        "power-supplies/keithley-2231a",
        "2231A-001 USB 适配器 / 虚拟串口",
        "专用 USB 适配器驱动；PySerial 或 VISA ASRL",
    ),
    Path(__file__).parent,
    deployment_components=("rs-visa", "keithley-usb"),
    serial_driver_managed=True,
)
