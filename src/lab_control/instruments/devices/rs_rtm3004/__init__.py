"""R&S RTM3004: independent protocol and documentation package."""

from pathlib import Path

from lab_control.domain.processing import ProcessingProfile
from lab_control.domain.voltage_waveform_metrics import VOLTAGE_WAVEFORM_STATISTICS_PROFILE
from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import Rtm3004Adapter
from .profile import rtm3004

DEFINITION = DeviceDefinition(
    rtm3004(),
    Rtm3004Adapter,
    DeviceDocumentation("R&S RTM3004", "oscilloscopes/rtm3004", "LAN / USB VISA", "64 位 VISA Runtime"),
    Path(__file__).parent,
    deployment_components=("rs-visa",),
    offline_processing=ProcessingProfile("frames", separate_channel_pngs=True),
    metric_profiles=(VOLTAGE_WAVEFORM_STATISTICS_PROFILE,),
)
