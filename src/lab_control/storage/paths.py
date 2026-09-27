"""Immutable save destinations. Constructing a context performs no filesystem writes."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from lab_control.domain.instruments import DeviceIdentity, ValidationError, jsonable

SaveKind = Literal["single_frame", "continuous"]


def local_now() -> datetime:
    return datetime.now().astimezone()


def filename_part(value: str, fallback: str = "unknown") -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:48].rstrip(" .")
    if not value:
        value = fallback
    if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", value, re.I):
        value = "_" + value
    return value


def _time_name(started_at: datetime) -> str:
    return started_at.strftime("%H-%M-%S-") + f"{started_at.microsecond // 1000:03d}"


def _validate_time(started_at: datetime) -> None:
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        raise ValidationError("Save start time must include a timezone")


@dataclass(frozen=True)
class BatchSaveContext:
    root: Path
    started_at: datetime = field(default_factory=lambda: local_now())
    batch_id: str = field(default_factory=lambda: uuid4().hex[:12])

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())
        _validate_time(self.started_at)

    @property
    def folder(self) -> Path:
        return (
            self.root
            / "批量采集"
            / self.started_at.strftime("%Y-%m-%d")
            / f"{_time_name(self.started_at)}_batch_{filename_part(self.batch_id)}"
        )


@dataclass(frozen=True)
class SaveContext:
    root: Path
    mode: str
    kind: SaveKind
    device_id: str = ""
    identity: DeviceIdentity | None = None
    adapter_id: str = ""
    started_at: datetime = field(default_factory=lambda: local_now())
    run_id: str = field(default_factory=lambda: uuid4().hex[:12])
    batch: BatchSaveContext | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())
        if self.mode not in {"electronic", "optical"} or self.kind not in {"single_frame", "continuous"}:
            raise ValidationError("Invalid save mode or type")
        if self.batch:
            object.__setattr__(self, "root", self.batch.root)
            object.__setattr__(self, "started_at", self.batch.started_at)
        _validate_time(self.started_at)

    @property
    def started_utc(self) -> str:
        return self.started_at.astimezone(UTC).isoformat()

    @property
    def folder(self) -> Path:
        identity = self.identity
        model = filename_part(
            (identity.model if identity else "").strip() or self.adapter_id or self.device_id,
            "unknown-device",
        )
        serial = identity.serial if identity else ""
        if serial.strip().upper() in {"", "0", "N/A", "UNKNOWN"}:
            serial = self.device_id or "unknown-id"
        device = f"{model}_{filename_part(serial)}_{self.kind}_{filename_part(self.run_id)}"
        if self.batch:
            return self.batch.folder / device
        return (
            self.root
            / "单设备采集"
            / self.started_at.strftime("%Y-%m-%d")
            / f"{_time_name(self.started_at)}_{device}"
        )

    def validate(self, mode: str, kind: SaveKind) -> None:
        if self.mode != mode or self.kind != kind:
            raise ValidationError("Save context does not match the requested mode/type")

    def prepare(self) -> Path:
        """Reserve a new leaf exclusively. Existing data is never overwritten."""
        folder = self.folder
        folder.mkdir(parents=True, exist_ok=False)
        metadata = {
            "version": 1,
            "run_id": self.run_id,
            "kind": self.kind,
            "mode": self.mode,
            "scope": "batch" if self.batch else "single_device",
            "batch_id": self.batch.batch_id if self.batch else None,
            "started_local": self.started_at.isoformat(),
            "started_utc": self.started_utc,
            "device_id": self.device_id,
            "adapter_id": self.adapter_id,
            "identity": jsonable(self.identity),
        }
        with (folder / "recording.json").open("x", encoding="utf-8") as stream:
            json.dump(metadata, stream, ensure_ascii=False, indent=2, allow_nan=False)
        return folder
