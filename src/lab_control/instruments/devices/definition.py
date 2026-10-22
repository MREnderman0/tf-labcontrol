"""Pure metadata contract for model packages; no driver or hardware access."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import CapabilitySet, DeviceIdentity, InstrumentAdapter
from lab_control.instruments.base import Profile, ScpiAdapter


@dataclass(frozen=True)
class DeviceDocumentation:
    title: str
    card: str
    connection: str
    driver: str


@dataclass(frozen=True)
class DeviceDefinition:
    profile: Profile
    adapter_type: type[ScpiAdapter]
    documentation: DeviceDocumentation
    root: Path
    socket_port: int | None = None
    deployment_components: tuple[str, ...] = ()
    factory: Callable[[ConnectionConfig], InstrumentAdapter] | None = None
    # Model-owned transport selection keeps framing and serial defaults beside
    # the model protocol while the registry remains the single dispatch point.
    transport_factory: Callable[[ConnectionConfig, Any], Any] | None = None
    capability_schema: Callable[[ConnectionConfig], CapabilitySet] | None = None
    discovery_factory: Callable[[], Any] | None = None
    identity_requires_adapter: bool = False
    discovery_identity: Callable[[Any, ConnectionConfig], DeviceIdentity] | None = None
    # Model-owned normalization for explicitly saved discovery endpoints.  The
    # common discovery service supplies only the current physical IPv4 sources;
    # transport selection and model-specific routing stay in the device pack.
    discovery_config: Callable[[ConnectionConfig, tuple[str, ...]], ConnectionConfig] | None = None
    # Optional workspace placement for power-supply model packages.  Existing
    # power supplies remain monitoring-only by default; a package can opt into
    # the dedicated management surface without changing its protocol profile.
    management_surface: bool = False
    monitoring_surface: bool = True
    # Explicit, model-owned read-only acceptance entry, also usable by frozen CLI.
    readonly_check: Callable[..., dict[str, Any]] | None = None

    @property
    def adapter_id(self) -> str:
        return self.profile.adapter_id
