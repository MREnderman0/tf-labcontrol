from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from lab_control.domain.instruments import ValidationError


@dataclass(frozen=True)
class ConnectionConfig:
    resource: str
    adapter: str = ""
    transport: str = "visa"
    baud_rate: int = 9600
    timeout: float = 3.0
    terminator: str = "\n"
    expected_serial: str = ""
    aliases: tuple[str, ...] = ()
    options: tuple[str, ...] = ()
    limits: dict[str, float] = field(default_factory=dict)
    sdk_path: str = ""
    identity_confirmed: bool = False
    data_resource: str = ""
    stream_format: str = ""
    stream_interval: float = 0.01

    def __post_init__(self) -> None:
        if self.transport not in {
            "visa",
            "serial",
            "tcp",
            "vicp",
            "tlpmx",
            "pm100d",
            "pico",
            "tserve",
            "mock",
        }:
            raise ValidationError("Unknown transport")
        if not self.resource or any(c in self.resource for c in "\r\n"):
            raise ValidationError("Invalid resource")
        if not 0.1 <= self.timeout <= 60 or not 300 <= self.baud_rate <= 3_000_000:
            raise ValidationError("Invalid timeout or baud rate")
        if self.terminator not in {"\n", "\r", "\r\n"}:
            raise ValidationError("Terminator must be LF, CR or CRLF")
        if self.stream_format not in {"", "P", "F"} or not 1e-6 <= self.stream_interval <= 1000:
            raise ValidationError("Invalid TSERVE stream format or interval")
        if not isinstance(self.limits, dict) or any(
            isinstance(value, bool)
            or not isinstance(value, (float, int))
            or not math.isfinite(value)
            or value < 0
            for value in self.limits.values()
        ):
            raise ValidationError("Limits must be finite non-negative numeric maxima")


def load_config(path: Path) -> tuple[ConnectionConfig, ...]:
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    devices = data.get("devices", [])
    if not isinstance(devices, list) or not devices:
        raise ValidationError("Configuration needs [[devices]] entries")
    known = {item.name for item in fields(ConnectionConfig)}
    result = []
    for item in devices:
        if set(item) - known:
            raise ValidationError(f"Unknown configuration keys: {set(item) - known}")
        item = dict(item)
        for key in ("aliases", "options"):
            item[key] = tuple(item.get(key, ()))
        result.append(ConnectionConfig(**item))
    return tuple(result)


def canonical_resource(resource: str) -> str:
    value = resource.strip().upper()
    match = re.fullmatch(r"(?:COM|ASRL)(\d+)(?:::INSTR)?", value)
    if match:
        return f"SERIAL:{int(match[1])}"
    socket_resource = re.fullmatch(r"TCPIP\d*::([^:]+)::(\d+)::SOCKET", value)
    direct_socket = re.fullmatch(r"TCP://([^:/]+):(\d+)/?", value)
    match = socket_resource or direct_socket
    if match:
        return f"SOCKET:{match[1]}:{int(match[2])}"
    return value


def config_from_record(record: dict[str, Any]) -> ConnectionConfig:
    return ConnectionConfig(**record["config"])
