"""K+K FXE discovery policy for explicitly saved address-list endpoints.

The address-list UI stores LAN endpoints in the common VISA Socket shape so
that older devices can share the same editor.  K+K does not speak VISA and
must be opened through the vendor library, so the model package changes only
the transport marker before the common scanner probes it.  The endpoint text
is kept intact for inventory continuity; the adapter converts the Socket
shape to the library's host form when opening the connection.
"""

from __future__ import annotations

import re
from dataclasses import replace

from lab_control.application.config import ConnectionConfig

_SOCKET_RESOURCE = re.compile(r"TCPIP\d*::([^:]+)::(\d+)::SOCKET", re.IGNORECASE)


def normalize_discovery_config(
    config: ConnectionConfig, _source_addresses: tuple[str, ...]
) -> ConnectionConfig:
    """Mark a saved K+K Socket endpoint for the official-library transport."""
    if config.transport not in {"visa", "tcp"}:
        return config
    if _SOCKET_RESOURCE.fullmatch(config.resource.strip()) is None:
        return config
    return replace(config, transport="kplusk", source_address="")

