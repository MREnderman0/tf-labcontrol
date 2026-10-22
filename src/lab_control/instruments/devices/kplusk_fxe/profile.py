"""Conservative K+K FXE metadata and read-only capability declarations."""

from __future__ import annotations

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import Capability, CapabilitySet
from lab_control.instruments.base import CommandSpec, Profile

OFFICIAL_DOWNLOADS = "https://kplusk-messtechnik.de/downloads/index.html"
OFFICIAL_LIBRARY_MANUAL = (
    "https://kplusk-messtechnik.de/downloads/files/Manual_KK-Library_MultiSource_Ver_21.1.0_25-06-03.pdf"
)


def _capabilities() -> tuple[Capability, ...]:
    # The library exposes reports and a few metadata getters.  It does not
    # expose a verified FXE configuration getter, so no setting or calibrated
    # measurement is declared here.
    return (
        Capability(
            "report",
            readable=True,
            source=OFFICIAL_LIBRARY_MANUAL,
            section="Multi_GetReport",
            verification="protocol_verified_read_only",
        ),
        Capability(
            "firmware_version",
            readable=True,
            source=OFFICIAL_LIBRARY_MANUAL,
            section="Multi_GetFirmwareVersion",
            verification="protocol_verified_read_only",
        ),
        Capability(
            "user_id",
            readable=True,
            source=OFFICIAL_LIBRARY_MANUAL,
            section="Multi_GetUserID",
            verification="protocol_verified_read_only",
        ),
    )


def capability_schema(config: ConnectionConfig) -> CapabilitySet:
    return CapabilitySet("frequency_counter", tuple(range(1, 25)), _capabilities())


def profile() -> Profile:
    capabilities = _capabilities()
    return Profile(
        adapter_id="kplusk.fxe",
        manufacturer=("K+K", "K+K Messtechnik"),
        models=("FXE", "FXE-compatible K+K counter"),
        category="frequency_counter",
        channels=tuple(range(1, 25)),
        source=OFFICIAL_DOWNLOADS,
        revision="K+K Library 21.2.2 / Python wrapper 2.1; read-only compatibility subset",
        commands={item.name: CommandSpec(item) for item in capabilities},
        status=("firmware_version", "user_id", "report"),
        identity_query="KPLUSK_LIBRARY_IDENTITY",
        error_query="",
        acquisition="scalar:frequency",
    )
