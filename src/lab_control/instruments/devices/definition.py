"""Pure metadata contract for model packages; no driver or hardware access."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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

    @property
    def adapter_id(self) -> str:
        return self.profile.adapter_id
