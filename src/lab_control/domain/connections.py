"""Pure connection identity and validation, without storage or runtime access."""

from __future__ import annotations

import ipaddress
import math
import re
from dataclasses import dataclass, field

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
    source_address: str = ""

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
            "kplusk",
            "mock",
        }:
            raise ValidationError("Unknown transport")
        if not self.resource or any(c in self.resource for c in "\r\n"):
            raise ValidationError("Invalid resource")
        if self.source_address:
            if self.transport != "tcp":
                raise ValidationError("本地网卡地址仅用于 TCP Socket 连接")
            try:
                source = ipaddress.ip_address(self.source_address)
            except (TypeError, ValueError) as exc:
                raise ValidationError("Invalid local source address") from exc
            if source.version != 4 or source.is_unspecified or source.is_loopback or source.is_multicast:
                raise ValidationError("Local source address must be a unicast IPv4 address")
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


def network_host(resource: str) -> str:
    """Extract a LAN host without importing device modules or opening a connection."""
    from urllib.parse import urlparse

    value = resource.strip()
    if value.upper().startswith("TCPIP"):
        parts = value.split("::")
        return parts[1].lower() if len(parts) >= 3 else ""
    if value.lower().startswith(("tcp://", "vicp://")):
        return urlparse(value).hostname or ""
    if value.lower().startswith("mock://"):
        return ""
    try:
        parsed = urlparse(value if "://" in value else "//" + value)
        # Literal addresses and URI endpoints are shared transport syntax.
        # Do not interpret arbitrary SDK names or serial identifiers as hosts.
        return str(ipaddress.ip_address(parsed.hostname or ""))
    except ValueError:
        return ""
