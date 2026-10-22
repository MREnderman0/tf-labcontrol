"""Ceyear 6362D discovery transport policy.

The common scanner owns enumeration and leases.  This module only turns an
explicit 6362D Socket endpoint into the model's TCP transport and chooses a
physical local source address when the host has competing virtual routes.
"""

from __future__ import annotations

import re
import socket
from dataclasses import replace
from typing import Callable

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import DeviceError

_SOCKET_RESOURCE = re.compile(r"TCPIP\d*::([^:]+)::(\d+)::SOCKET", re.IGNORECASE)
_SOCKET_PORT = 10001


def _socket_target(config: ConnectionConfig) -> tuple[str, int] | None:
    if config.transport not in {"visa", "tcp"}:
        return None
    match = _SOCKET_RESOURCE.fullmatch(config.resource.strip())
    if not match or int(match.group(2)) != _SOCKET_PORT:
        return None
    return match.group(1), int(match.group(2))


def route_source(host: str, port: int, source_address: str) -> str | None:
    """Resolve the route selected by a source address without sending data."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((source_address, 0))
        sock.connect((host, port))
        selected = sock.getsockname()[0]
        return selected if selected == source_address else None
    except OSError:
        return None
    finally:
        sock.close()


def select_source_address(
    host: str,
    port: int,
    source_addresses: tuple[str, ...],
    probe: Callable[[str, int, str], str | None] = route_source,
) -> str | None:
    """Choose the first deterministic physical source with a valid route."""
    for source in sorted({str(value).strip() for value in source_addresses if str(value).strip()}):
        selected = probe(host, port, source)
        if selected:
            return selected
    return None


def normalize_discovery_config(
    config: ConnectionConfig, source_addresses: tuple[str, ...]
) -> ConnectionConfig:
    """Normalize a saved 6362D Socket endpoint before its identity probe."""
    target = _socket_target(config)
    if target is None:
        return config
    host, port = target
    if config.source_address:
        return replace(config, transport="tcp")
    source = select_source_address(host, port, source_addresses)
    if source is None:
        raise DeviceError(
            "6362D requires a reachable physical Ethernet source address for this Socket",
            "source_address_required",
        )
    return replace(config, transport="tcp", source_address=source)
