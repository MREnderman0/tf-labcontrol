"""K+K FXE discovery policy for explicitly saved address-list endpoints.

The address-list UI stores LAN endpoints in the common VISA Socket shape so
that older devices can share the same editor.  K+K does not speak VISA and
must be opened through the vendor library, so the model package converts the
saved endpoint to the same host resource used by the startup configuration.
"""

from __future__ import annotations

import re
from dataclasses import replace

from lab_control.domain.connections import ConnectionConfig

_SOCKET_RESOURCE = re.compile(r"TCPIP\d*::([^:]+)::(\d+)::SOCKET", re.IGNORECASE)


def normalize_discovery_config(
    config: ConnectionConfig, _source_addresses: tuple[str, ...]
) -> ConnectionConfig:
    """Convert a saved K+K Socket endpoint to the official-library transport."""
    if config.transport not in {"visa", "tcp"}:
        return config
    match = _SOCKET_RESOURCE.fullmatch(config.resource.strip())
    if match is None:
        return config
    host, port = match.groups()
    # FXE's address-list port is the library's default service port.  Use the
    # same bare-host resource as devices.toml so the two discovery paths merge
    # into one inventory record.  Keep non-default ports in the model-owned
    # Socket form; the adapter passes their explicit host:port to the library.
    resource = host if int(port) == 10001 else config.resource
    return replace(config, resource=resource, transport="kplusk", source_address="")
