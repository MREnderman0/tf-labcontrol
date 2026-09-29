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
    usb_instance_id: str = ""
    server_path: str = ""

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
            isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value)
            for value in self.limits.values()
        ):
            raise ValidationError(
                "Limits must be finite numeric maxima (signed units such as dBm are allowed)"
            )


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


def load_startup_config(path: Path | None = None) -> tuple[ConnectionConfig, ...]:
    """Load the explicit file or the user's single default manifest, without hardware I/O.

    Do not glob config directories: backups and alternate transports are not opt-in
    scan targets. An invalid manifest must fail visibly rather than hide devices.
    """
    from lab_control.storage.repository import application_home

    if path is not None:
        return load_config(path)
    default = application_home() / "config" / "devices.toml"
    return load_config(default) if default.exists() else ()


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


def save_startup_device(config: ConnectionConfig, path: Path) -> None:
    """Persist one explicitly selected asset, retaining unrelated manifest text.

    The caller verifies the instrument identity. This function does no hardware I/O.
    Ambiguous resource ownership and unsupported TOML layouts fail without replacement.
    """
    import json
    import os
    import tempfile
    from dataclasses import asdict

    from lab_control.application.leases import FileLease

    if not config.adapter or not config.expected_serial:
        raise ValidationError("Persist an exact adapter and expected instrument serial")
    path.parent.mkdir(parents=True, exist_ok=True)
    lease = FileLease(path.with_suffix(path.suffix + ".lock"))
    try:
        original = path.read_text(encoding="utf-8") if path.exists() else ""
        existing = load_config(path) if original else ()
        headers = list(re.finditer(r"(?m)^\s*\[\[devices\]\][ \t]*(?:#[^\n]*)?$", original))
        if len(headers) != len(existing):
            raise ValidationError("Unsupported devices manifest layout; original retained")
        retained = original[: headers[0].start()] if headers else original
        expected = []
        for index, item in enumerate(existing):
            same_asset = (item.adapter, item.expected_serial) == (config.adapter, config.expected_serial)
            same_address = canonical_resource(item.resource) == canonical_resource(config.resource)
            if same_address and not same_asset:
                raise ValidationError("Configured resource belongs to another asset; original retained")
            if not same_asset:
                end = headers[index + 1].start() if index + 1 < len(headers) else len(original)
                retained += original[headers[index].start() : end]
                expected.append(item)

        def literal(value: Any) -> str:
            if isinstance(value, dict):
                return "{ " + ", ".join(f"{json.dumps(k)} = {literal(v)}" for k, v in value.items()) + " }"
            return json.dumps(value, ensure_ascii=False, allow_nan=False)

        block = "[[devices]]\n" + "".join(f"{k} = {literal(v)}\n" for k, v in asdict(config).items())
        result = retained.rstrip() + "\n\n" + block
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(result)
                stream.flush()
                os.fsync(stream.fileno())
            if load_config(Path(temporary)) != (*expected, config):
                raise ValidationError("Manifest round-trip differs; original retained")
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    finally:
        lease.close()
