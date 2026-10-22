"""Persisted, explicitly selected LAN instrument addresses.

The address list is deliberately small in scope: it stores exact IPv4
endpoints and never expands an address into a subnet or port scan.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from uuid import uuid4

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import ValidationError
from lab_control.storage.repository import Repository

STATIC_ADDRESSES_KEY = "discovery/static-addresses"
DEFAULT_PORT = 10001
DEFAULT_ADAPTER = "ceyear.6362d"


def _address(value: str) -> str:
    try:
        parsed = ipaddress.IPv4Address(str(value).strip())
    except (ValueError, TypeError) as exc:
        raise ValidationError("静态地址必须是有效的 IPv4 地址") from exc
    if parsed.is_unspecified or parsed.is_loopback or parsed.is_multicast:
        raise ValidationError("静态地址不能使用未指定、回环或组播地址")
    return str(parsed)


def _serial_port(value: str) -> str:
    match = re.fullmatch(r"(?:COM|ASRL)\s*(\d+)(?:::INSTR)?", str(value).strip(), re.I)
    if not match:
        raise ValidationError("串口地址应填写 COM3 这样的端口")
    number = int(match.group(1))
    if not 1 <= number <= 256:
        raise ValidationError("串口号必须是 COM1 ～ COM256")
    return f"COM{number}"


@dataclass(frozen=True)
class StaticAddress:
    id: str
    address: str
    port: int = DEFAULT_PORT
    adapter: str = DEFAULT_ADAPTER
    timeout: float = 3.0
    label: str = ""
    kind: str = "ip"
    baud_rate: int = 9600
    source_address: str = ""

    def __post_init__(self) -> None:
        if self.kind not in {"ip", "serial"}:
            raise ValidationError("设备地址类型无效")
        object.__setattr__(
            self,
            "address",
            _serial_port(self.address) if self.kind == "serial" else _address(self.address),
        )
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValidationError("静态地址端口必须是 1 ～ 65535")
        if not isinstance(self.adapter, str) or len(self.adapter) > 120:
            raise ValidationError("设备适配器名称无效")
        if type(self.timeout) not in (int, float) or not 0.1 <= float(self.timeout) <= 60:
            raise ValidationError("静态地址通信超时必须是 0.1 ～ 60 秒")
        if not isinstance(self.label, str) or len(self.label) > 120:
            raise ValidationError("静态地址备注过长")
        if type(self.baud_rate) is not int or not 300 <= self.baud_rate <= 3_000_000:
            raise ValidationError("串口波特率必须是 300 ～ 3000000")
        if self.source_address:
            if self.kind != "ip":
                raise ValidationError("串口连接不使用本地网卡地址，请清空该项")
            try:
                source = ipaddress.ip_address(self.source_address)
            except (TypeError, ValueError) as exc:
                raise ValidationError("本地网卡地址必须是有效的 IPv4 地址") from exc
            if source.version != 4 or source.is_unspecified or source.is_loopback or source.is_multicast:
                raise ValidationError("本地网卡地址必须是单播 IPv4 地址")

    @property
    def resource(self) -> str:
        if self.kind == "serial":
            return self.address
        return f"TCPIP0::{self.address}::{self.port}::SOCKET"

    def config(self) -> ConnectionConfig:
        return ConnectionConfig(
            self.resource,
            self.adapter,
            transport=("serial" if self.kind == "serial" else "tcp" if self.source_address else "visa"),
            baud_rate=self.baud_rate,
            timeout=float(self.timeout),
            source_address=self.source_address,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "address": self.address,
            "port": self.port,
            "adapter": self.adapter,
            "timeout": self.timeout,
            "label": self.label,
            "kind": self.kind,
            "baud_rate": self.baud_rate,
            "source_address": self.source_address,
        }

    @classmethod
    def from_value(cls, value: object) -> "StaticAddress":
        if isinstance(value, str):
            return cls(uuid4().hex, value)
        if not isinstance(value, dict):
            raise ValidationError("静态地址列表数据格式无效")
        address = str(value.get("address", ""))
        kind = str(
            value.get(
                "kind", "serial" if re.fullmatch(r"(?:COM|ASRL)\s*\d+(?:::INSTR)?", address, re.I) else "ip"
            )
        )
        return cls(
            str(value.get("id") or uuid4().hex),
            address,
            int(value.get("port", DEFAULT_PORT)),
            str(value.get("adapter", DEFAULT_ADAPTER)),
            float(value.get("timeout", 3.0)),
            str(value.get("label", "")),
            kind,
            int(value.get("baud_rate", 9600)),
            str(value.get("source_address", "")),
        )


class StaticAddressStore:
    """Repository-backed list used by both the UI and discovery providers."""

    def __init__(self, repository: Repository):
        self.repository = repository

    def entries(self) -> tuple[StaticAddress, ...]:
        raw = self.repository.load_workspace(STATIC_ADDRESSES_KEY, [])
        if raw is None:
            return ()
        if isinstance(raw, dict):
            raw = raw.get("items", [])
        if not isinstance(raw, list):
            raise ValidationError("静态地址列表数据格式无效")
        result: list[StaticAddress] = []
        seen: set[str] = set()
        for value in raw:
            entry = StaticAddress.from_value(value)
            # Keep a damaged/duplicated old record from making every scan probe
            # the same endpoint more than once.
            key = entry.resource.upper() + "\0" + entry.adapter
            if key in seen:
                continue
            seen.add(key)
            result.append(entry)
        return tuple(result)

    def _save(self, entries: tuple[StaticAddress, ...]) -> None:
        self.repository.save_workspace(STATIC_ADDRESSES_KEY, [entry.as_dict() for entry in entries])

    def add(
        self,
        address: str,
        *,
        port: int = DEFAULT_PORT,
        adapter: str = DEFAULT_ADAPTER,
        timeout: float = 3.0,
        label: str = "",
        kind: str = "ip",
        baud_rate: int = 9600,
        source_address: str = "",
    ) -> StaticAddress:
        entry = StaticAddress(
            uuid4().hex,
            address,
            port,
            adapter,
            timeout,
            label.strip(),
            kind,
            baud_rate,
            source_address.strip(),
        )
        entries = self.entries()
        if any(item.resource.upper() == entry.resource.upper() for item in entries):
            raise ValidationError("该静态地址已在列表中")
        self._save((*entries, entry))
        return entry

    def remove(self, ids: set[str] | tuple[str, ...] | list[str]) -> int:
        target = {str(value) for value in ids}
        entries = self.entries()
        kept = tuple(entry for entry in entries if entry.id not in target)
        self._save(kept)
        return len(entries) - len(kept)

    def by_ids(self, ids: tuple[str, ...] | list[str] | set[str]) -> tuple[StaticAddress, ...]:
        selected = {str(value) for value in ids}
        return tuple(entry for entry in self.entries() if entry.id in selected)
