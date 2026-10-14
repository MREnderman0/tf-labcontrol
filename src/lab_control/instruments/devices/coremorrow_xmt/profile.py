"""Conservative metadata for the observed CoreMorrow XMT protocol family."""

from __future__ import annotations

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import Capability, CapabilitySet, ValidationError
from lab_control.instruments.base import CommandSpec, Profile

OFFICIAL_SITE = "https://www.coremorrow.com/"
OFFICIAL_E09_MANUAL = "https://www.coremorrow.com/testrecord/finaltest/usermanual/E09%20PZT-User-Manual.pdf"
VENDOR_HEADER_EVIDENCE = "Installed XMT_DLL_SER.h (CoreMorrow Control Software 25.09.26)"


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
    return CapabilitySet("multimeter", configured_channels(config), _capabilities())


def profile() -> Profile:
    capabilities = _capabilities()
    return Profile(
        adapter_id="coremorrow.xmt",
        manufacturer=("CoreMorrow", "芯明天"),
        models=("XMT-compatible controller (exact model pending)",),
        category="multimeter",
        channels=tuple(range(1, 9)),
        source=OFFICIAL_SITE,
        revision="XMT serial frame observed from vendor DLL 25.09.26; exact model/E09 applicability pending",
        commands={item.name: CommandSpec(item) for item in capabilities},
        status=("power_config",),
        identity_query="XMT_READ_POWER_CONFIG",
        error_query="",
        acquisition="",
    )

