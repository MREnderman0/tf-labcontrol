"""RIGOL DS1202Z（待确认后缀）/ DS1202Z-E: independent protocol and documentation package."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import Ds1202zeAdapter, rigol

DEFINITION = DeviceDefinition(
    rigol(),
    Ds1202zeAdapter,
    DeviceDocumentation(
        "RIGOL DS1202Z（待确认后缀）/ DS1202Z-E",
        "oscilloscopes/rigol-ds1202z",
        "DS1202Z-E：USB / LAN VISA",
        "64 位 VISA Runtime",
    ),
    Path(__file__).parent,
    deployment_components=("rs-visa",),
)
