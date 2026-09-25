"""Use a measured transport preference only in its qualified environment."""

from __future__ import annotations

import hashlib
import platform
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from lab_control.application.config import ConnectionConfig
from lab_control.discovery.addresses import network_host
from lab_control.instruments.registry import DEVICE_DEFINITIONS
from lab_control.storage.repository import Repository
from lab_control.transport.visa import VisaRuntime


@lru_cache(maxsize=8)
def _library_hash(path: str, size: int, modified: int) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def environment_signature(runtime: VisaRuntime) -> dict[str, Any]:
    import pyvisa

    library = runtime.manager.visalib
    path = Path(str(library.library_path)).resolve()
    stat = path.stat()
    return {
        "host": platform.node(),
        "system": platform.platform(),
        "machine": platform.machine(),
        "pyvisa": pyvisa.__version__,
        "visa_library": str(path),
        "visa_sha256": _library_hash(str(path), stat.st_size, stat.st_mtime_ns),
    }


def preference_key(adapter: str, serial: str) -> str:
    return f"qualified-transport/{adapter}/{serial}"


def qualified_connection(
    record: dict[str, Any],
    config: ConnectionConfig,
    repository: Repository,
    runtime: VisaRuntime,
) -> tuple[ConnectionConfig, str]:
    """Return a candidate, not a verified identity. Session opening must identify it."""
    definition = DEVICE_DEFINITIONS.get(config.adapter)
    identity = record.get("identity", {})
    serial = identity.get("serial", "")
    if (
        record.get("primaryResourceExplicit")
        or record.get("identityConflict")
        or not definition
        or not definition.socket_port
        or not serial
        or config.transport != "visa"
        or not network_host(config.resource)
    ):
        return config, ""
    qualified = repository.load_workspace(preference_key(config.adapter, serial), {})
    if not qualified:
        return config, ""
    revision = getattr(definition.adapter_type, "acquisition_revision", "")
    try:
        matches = (
            qualified.get("passed") is True
            and qualified.get("serial") == serial
            and qualified.get("firmware") == identity.get("firmware")
            and qualified.get("adapter_revision") == revision
            and qualified.get("port") == definition.socket_port
            and qualified.get("environment") == environment_signature(runtime)
        )
    except (OSError, AttributeError):
        matches = False
    if not matches:
        return config, ""
    resource = f"TCPIP0::{network_host(config.resource)}::{definition.socket_port}::SOCKET"
    return replace(
        config,
        resource=resource,
        aliases=tuple(dict.fromkeys((config.resource, *config.aliases))),
        expected_serial=serial,
    ), qualified["firmware"]
