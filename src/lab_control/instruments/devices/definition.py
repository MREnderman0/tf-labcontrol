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
    capability_schema: Callable[[ConnectionConfig], CapabilitySet] | None = None
    discovery_factory: Callable[[], Any] | None = None
    identity_requires_adapter: bool = False
    discovery_identity: Callable[[Any, ConnectionConfig], DeviceIdentity] | None = None

    @property
    def adapter_id(self) -> str:
        return self.profile.adapter_id
