from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from lab_control.domain.connections import ConnectionConfig


@dataclass(frozen=True)
class DiscoveryCandidate:
    config: ConnectionConfig
    source: str
    descriptor: dict[str, Any] = field(default_factory=dict)
    safe_scpi: bool = False
    error_code: str = ""
    error: str = ""


class DiscoveryProvider(Protocol):
    name: str

    def enumerate(self) -> tuple[DiscoveryCandidate, ...]: ...
