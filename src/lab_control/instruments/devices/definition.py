"""Pure metadata contract for model packages; no driver or hardware access."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import CapabilitySet, DeviceIdentity, InstrumentAdapter
from lab_control.instruments.base import Profile, ScpiAdapter


@dataclass(frozen=True)
class DeviceDocumentation:
    title: str
    card: str
    connection: str
    driver: str


@dataclass(frozen=True)
class DeviceModel:
    """Identity and documentation metadata for devices without a SCPI profile."""

    adapter_id: str
    manufacturer: tuple[str, ...]
    models: tuple[str, ...]
    category: str
    source: str
    revision: str
    acquisition: str = ""


@dataclass(frozen=True)
class DeviceDefinition:
    profile: Profile | None
    adapter_type: type[ScpiAdapter] | None
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
    model: DeviceModel | None = None
    discovery_enumerator: Callable[[ConnectionConfig], tuple[str, ...]] | None = None
    discovery_transports: tuple[str, ...] = ()
    # Enumeration may publish a candidate, but these transports require an
    # explicit connection before identity probing may construct an adapter.
    # This is independent of identity_requires_adapter (how to probe).
    discovery_connection_only_transports: tuple[str, ...] = ()
    supported_transports: tuple[str, ...] = ()
    acquisition_kinds: tuple[str, ...] = ()
    acquisition_initiate_default: bool = False
    acquisition_initiate_editable: bool = True
    acquisition_requires_confirmation: bool = False
    serial_driver_managed: bool = False

    def __post_init__(self) -> None:
        if self.profile is None:
            if self.model is None or self.factory is None or self.capability_schema is None:
                raise ValueError("SDK definitions require model metadata, factory and capability_schema")
            if self.adapter_type is not None:
                raise ValueError("SDK definitions must not declare a SCPI adapter type")
        elif self.adapter_type is None:
            raise ValueError("SCPI definitions require an adapter type")
        elif self.model is not None and self.model.adapter_id != self.profile.adapter_id:
            raise ValueError("Model metadata and profile adapter IDs disagree")

    @property
    def category(self) -> str:
        if self.profile is not None:
            return self.profile.category
        assert self.model is not None
        return self.model.category

    @property
    def adapter_id(self) -> str:
        if self.profile is not None:
            return self.profile.adapter_id
        assert self.model is not None
        return self.model.adapter_id
