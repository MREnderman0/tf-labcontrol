"""Offline processing capabilities and history entries, independent of hardware."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


@dataclass(frozen=True)
class ProcessingProfile:
    kind: Literal["scalar", "frames"]
    power_units: bool = False
    separate_channel_pngs: bool = False
    # Model-owned per-frame telemetry is preserved but does not split curves.
    volatile_configuration_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecordedDevice:
    folder: Path
    metadata: dict[str, Any]
    summary: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def label(self) -> str:
        identity = self.metadata.get("identity") or {}
        model = identity.get("model") or self.metadata.get("adapter_id") or "未知设备"
        slot = self.metadata.get("slot_label", "")
        return f"{slot} · {model}" if slot else model


@dataclass(frozen=True)
class RecordingEntry:
    folder: Path
    scope: str
    started: str
    devices: tuple[RecordedDevice, ...]

    @property
    def key(self) -> str:
        return str(self.folder)

    @property
    def name(self) -> str:
        return self.folder.name
