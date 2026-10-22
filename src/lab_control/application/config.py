from __future__ import annotations

import re
import tomllib
from dataclasses import fields
from pathlib import Path
from typing import Any

from lab_control.domain.connections import ConnectionConfig as ConnectionConfig
from lab_control.domain.connections import canonical_resource as canonical_resource
from lab_control.domain.instruments import ValidationError


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
