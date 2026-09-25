"""Pure registration metadata; importing never opens hardware."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import WaveRunner9404MAdapter
from .profile import profile

DEFINITION = DeviceDefinition(
    profile(),
    WaveRunner9404MAdapter,
    DeviceDocumentation(
        "Teledyne LeCroy WaveRunner 9404M",
        "oscilloscopes/waverunner-9404m",
        "LAN PyVISA/VXI-11（仪器选择 LXI）",
        "64 位 VISA Runtime",
    ),
    Path(__file__).parent,
    deployment_components=("rs-visa",),
)
