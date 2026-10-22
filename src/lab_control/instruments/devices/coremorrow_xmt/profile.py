"""Metadata for the observed CoreMorrow XMT protocol family."""

from __future__ import annotations

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import Capability, CapabilitySet, ValidationError
from lab_control.instruments.base import CommandSpec, Profile, enum, parameter

from .protocol import WAVE_FREQUENCY_MAX, WAVE_FREQUENCY_MIN

OFFICIAL_SITE = "https://www.coremorrow.com/"
OFFICIAL_E00_PAGE = "https://www.coremorrow.com/en/proshow-26-313-1.html"
OFFICIAL_E00_SOFTWARE_MANUAL = "https://www.coremorrow.com/PIEZO_PRODUCT/catalog/en/Piezo%20Controller%20Software%20User%20Manual_CoreMorrow.pdf"
OFFICIAL_E18_MANUAL = (
    "https://www.coremorrow.com/PIEZO_PRODUCT/catalog/en/E18_Software_User_Manual_CoreMorrow.pdf"
)
VENDOR_HEADER_EVIDENCE = "Installed XMT_DLL_SER.h (CoreMorrow Control Software 25.09.26)"

# E00.C3-13's positive control envelope. The vendor table lists the
# E03.00.13 output as -20..+120 V; the compatibility page intentionally
# exposes the commonly used 0..120 V positive range and keeps negative output
# unavailable until a signed-output workflow is separately verified.
VOLTAGE_MIN = 0.0
VOLTAGE_MAX = 120.0


def configured_channels(config: ConnectionConfig) -> tuple[int, ...]:
    values = [item.split("=", 1)[1] for item in config.options if item.lower().startswith("channels=")]
    if len(values) > 1:
        raise ValidationError("coremorrow.xmt accepts at most one channels option")
    try:
        count = int(values[0]) if values else 3
    except ValueError as exc:
        raise ValidationError("coremorrow.xmt channels must be an integer") from exc
    if count < 1 or count > 8:
        raise ValidationError("coremorrow.xmt channels must be in [1, 8]")
    return tuple(range(1, count + 1))


def _capabilities() -> tuple[Capability, ...]:
    return (
        Capability(
            "control_state",
            readable=True,
            source=VENDOR_HEADER_EVIDENCE,
            section="Software command journal; no verified mode or waveform query in XMT_DLL_SER.h",
            verification="no_verified_mode_or_waveform_readback; explicit_unknown_with_command_journal",
        ),
        Capability(
            "standard_waveform",
            {
                "waveform": enum("SIN", "SQUARE", "TRIANGLE", "SAWTOOTH"),
                "frequency": parameter("Hz", WAVE_FREQUENCY_MIN, WAVE_FREQUENCY_MAX),
                "amplitude": parameter("V", 0.0001, VOLTAGE_MAX),
                "offset": parameter("V", VOLTAGE_MIN, VOLTAGE_MAX),
            },
            writable=True,
            channel_scoped=True,
            source=OFFICIAL_E00_PAGE,
            section="XMT_COMMAND_WaveSetMultWave B3=15; Z/F/S/J; envelope offset +/- Vpp/2",
            verification="vendor_encoder_verified; command_has_no_ack; physical_output_pending",
        ),
        Capability(
            "waveform_stop",
            writable=True,
            channel_scoped=True,
            source=VENDOR_HEADER_EVIDENCE,
            section="XMT_COMMAND_WaveSetMultWaveStop B3=17",
            verification="vendor_encoder_verified_no_ack",
        ),
        Capability(
            "normal_mode",
            writable=True,
            source=VENDOR_HEADER_EVIDENCE,
            section="Stop standard waveforms, restore saved DC values with bounded readback",
            verification="software_readback; physical_output_pending",
        ),
        Capability(
            "voltage",
            {"value": parameter("V", VOLTAGE_MIN, VOLTAGE_MAX)},
            readable=True,
            writable=True,
            channel_scoped=True,
            source=OFFICIAL_E00_SOFTWARE_MANUAL,
            section="XMT_COMMAND_SinglePoint(B3=0,B4=0); B3=51 readback",
            verification="official_e00_c_e03_00_13_positive_envelope_0_to_120V; wire_encoder_verified; physical_output_pending",
        ),
        Capability(
            "measured_voltage",
            readable=True,
            channel_scoped=True,
            source=OFFICIAL_E00_SOFTWARE_MANUAL,
            section="XMT_COMMAND_ReadMultChannelMoveOrVolt(B3=51,B4=0)",
            verification="wire_verified_no_load_readback; physical_output_pending",
        ),
        Capability(
            "power_config",
            readable=True,
            source=OFFICIAL_SITE,
            section="XMT_COMMAND_ReadPowerConfig(B3=80,B4=0)",
            verification="protocol_verified_read_only",
        ),
        Capability(
            "raw_channel_data",
            readable=True,
            channel_scoped=True,
            source=OFFICIAL_SITE,
            section="XMT_COMMAND_ReadData(B3=5,B4=0)",
            verification="protocol_verified_read_only",
        ),
    )


def capability_schema(config: ConnectionConfig) -> CapabilitySet:
    return CapabilitySet("power_supply", configured_channels(config), _capabilities())


def profile() -> Profile:
    capabilities = _capabilities()
    return Profile(
        adapter_id="coremorrow.xmt",
        manufacturer=("CoreMorrow", "芯明天"),
        models=("XMT-compatible controller (exact model pending)",),
        category="power_supply",
        channels=tuple(range(1, 9)),
        source=OFFICIAL_E00_PAGE,
        revision="E00.C3-13 positive 0..120 V control envelope from vendor E00.C table; serial frame and encoder verified, exact instrument serial and physical output pending",
        commands={item.name: CommandSpec(item) for item in capabilities},
        status=("power_config",),
        identity_query="XMT_READ_POWER_CONFIG",
        error_query="",
        acquisition="",
    )
