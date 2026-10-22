"""ITECH IT6322: independent protocol and documentation package."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import It6322Adapter, it6322

DEFINITION = DeviceDefinition(
    it6322(),
    It6322Adapter,
    DeviceDocumentation(
        "ITECH IT6322",
        "power-supplies/it6322",
        "RS-232 / 已安装 USB 或 GPIB 接口",
        "相应接口驱动；VISA 或 PySerial",
    ),
    Path(__file__).parent,
    deployment_components=("rs-visa", "serial-interface", "gpib-interface"),
)
