"""LAN discovery on current Ethernet networks; no address assignment or model protocols."""

from __future__ import annotations

import ipaddress
import json
import socket
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from lab_control.application.config import ConnectionConfig
from lab_control.application.static_addresses import StaticAddressStore
from lab_control.discovery.service import DiscoveryCandidate, Inventory
from lab_control.domain.instruments import DeviceError, ValidationError
from lab_control.transport.visa import VisaRuntime


@dataclass(frozen=True)
class LanDiscoverySettings:
    enabled: bool = False
    interfaces: tuple[str, ...] = ()
    networks: tuple[str, ...] = ()
    timeout: float = 3.0
    auto_ethernet: bool = False

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool or not 0.1 <= self.timeout <= 15:
            raise ValidationError("LAN discovery timeout must be 0.1..15 s")
        if type(self.auto_ethernet) is not bool:
            raise ValidationError("LAN auto_ethernet must be a boolean")
        if self.auto_ethernet and self.interfaces:
            raise ValidationError("Select automatic Ethernet discovery or explicit interface GUIDs")
        if self.enabled and not (self.auto_ethernet or self.interfaces or self.networks):
            raise ValidationError("Select lab interface GUIDs or explicit LAN networks")
        for network in self.networks:
            ipaddress.IPv4Network(network, strict=False)


def load_lan_settings(root: Path) -> LanDiscoverySettings:
    path = root / "config" / "lan-discovery.toml"
    if not path.exists():
        return LanDiscoverySettings(enabled=sys.platform == "win32", auto_ethernet=sys.platform == "win32")
    data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    if set(data) - {"enabled", "interfaces", "networks", "timeout", "auto_ethernet"}:
        raise ValidationError("Unknown LAN discovery setting")
    for field in ("interfaces", "networks"):
        items = data.get(field, [])
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            raise ValidationError(f"LAN {field} must be a list of strings")
        data[field] = tuple(items)
    return LanDiscoverySettings(**data)


def ethernet_networks() -> tuple[str, ...]:
    """Resolve up physical Ethernet adapters each scan, including newly attached USB NICs."""
    if sys.platform != "win32":
        raise DeviceError("Automatic Ethernet discovery requires Windows", "unsupported")
    # HardwareInterface + type 6 excludes WLAN, loopback, VPN and virtual switches.
    # Never run network-setting commands, DHCP, or unicast subnet/port sweeps here.
    script = "$a=@{}; Get-NetAdapter -Physical | Where-Object {$_.Status -eq 'Up' -and $_.InterfaceType -eq 6} | ForEach-Object {$a[$_.ifIndex]=$true}; @(Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$a.ContainsKey($_.InterfaceIndex) -and $_.AddressState -eq 'Preferred'} | ForEach-Object {[pscustomobject]@{ip=$_.IPAddress;prefix=$_.PrefixLength}}) | ConvertTo-Json -Compress"
    process = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
        creationflags=0x08000000,
    )
    rows = json.loads(process.stdout or "[]")
    if isinstance(rows, dict):
        rows = [rows]
    networks = set()
    for row in rows:
        address = ipaddress.IPv4Address(row["ip"])
        if address.is_unspecified or address.is_loopback or address.is_multicast:
            continue
        networks.add(str(ipaddress.IPv4Network(f"{address}/{row['prefix']}", strict=False)))
    return tuple(sorted(networks))


def interface_networks(interfaces: tuple[str, ...]) -> tuple[str, ...]:
    if not interfaces:
        return ()
    if sys.platform != "win32":
        raise DeviceError("Use explicit LAN networks on this platform", "unsupported")
    # Constant script: no user text is interpolated into PowerShell.
    script = "$a=@{}; Get-NetAdapter | ForEach-Object {$a[$_.ifIndex]=$_.InterfaceGuid}; @(Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.AddressState -eq 'Preferred'} | ForEach-Object {[pscustomobject]@{guid=$a[$_.InterfaceIndex];ip=$_.IPAddress;prefix=$_.PrefixLength}}) | ConvertTo-Json -Compress"
    process = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
        creationflags=0x08000000,
    )
    rows = json.loads(process.stdout or "[]")
    if isinstance(rows, dict):
        rows = [rows]
    selected = {item.strip("{}").lower() for item in interfaces}
    return tuple(
        str(ipaddress.IPv4Network(f"{row['ip']}/{row['prefix']}", strict=False))
        for row in rows
        if str(row.get("guid", "")).strip("{}").lower() in selected
    )


def discovery_networks(
    settings: LanDiscoverySettings,
) -> tuple[tuple[ipaddress.IPv4Network, ...], tuple[ipaddress.IPv4Network, ...]]:
    attached = ethernet_networks() if settings.auto_ethernet else interface_networks(settings.interfaces)
    use_attached = settings.auto_ethernet or bool(settings.interfaces)
    networks = tuple(
        ipaddress.IPv4Network(value, strict=False)
        for value in (attached if use_attached else settings.networks)
    )
    restriction = tuple(ipaddress.IPv4Network(value, strict=False) for value in settings.networks)
    if not networks and not settings.auto_ethernet:
        raise DeviceError("Selected lab Ethernet interface has no usable IPv4 address", "network_unavailable")
    return networks, restriction


class RememberedLanDiscoveryProvider:
    """Recheck a verified primary address even when its service does not advertise.

    A saved address is a query target, never fresh presence evidence. Only the
    common scanner's identity query can make it current; no historical aliases
    or speculative socket ports are added.
    """

    name = "remembered_lan"

    def __init__(self, inventory: Inventory, settings: LanDiscoverySettings):
        self.inventory, self.settings = inventory, settings

    def enumerate(self) -> tuple[DiscoveryCandidate, ...]:
        if not self.settings.enabled:
            return ()
        networks, restriction = discovery_networks(self.settings)
        if not networks:
            return ()
        found = []
        for record in self.inventory.all():
            config = record.get("config", {})
            resource = config.get("resource", "")
            entry = record.get("candidates", {}).get(resource, {})
            identity_key = record.get("identityKey")
            if (
                not identity_key
                or record.get("identityConflict")
                or record.get("discoveryState") == "conflict"
                or entry.get("retired")
                or entry.get("verifiedIdentityKey") != identity_key
                or config.get("transport") != "visa"
                or not config.get("adapter")
                or not resource.upper().startswith("TCPIP")
                or not resource.upper().endswith(("::INSTR", "::SOCKET"))
            ):
                continue
            try:
                addresses = {
                    ipaddress.IPv4Address(row[4][0])
                    for row in socket.getaddrinfo(resource.split("::")[1], None, socket.AF_INET)
                }
            except (OSError, ValueError, IndexError):
                continue
            if not addresses or not all(
                any(address in net for net in networks)
                and (not restriction or any(address in net for net in restriction))
                for address in addresses
            ):
                continue
            # DHCP may now assign this address to another instrument. Carry no
            # prior model, serial, limits or aliases into the identity probe.
            found.append(
                DiscoveryCandidate(
                    ConnectionConfig(resource, timeout=config.get("timeout", self.settings.timeout)),
                    self.name,
                    {"saved_primary": True, "scope": [str(net) for net in networks]},
                    safe_scpi=True,
                )
            )
        return tuple(found)


class LanDiscoveryProvider:
    name = "lan"

    def __init__(self, runtime: VisaRuntime, settings: LanDiscoverySettings):
        self.runtime, self.settings = runtime, settings

    def enumerate(self) -> tuple[DiscoveryCandidate, ...]:
        if not self.settings.enabled:
            return ()
        networks, restriction = discovery_networks(self.settings)
        if not networks:
            return ()
        found: dict[str, DiscoveryCandidate] = {}
        for resource in self.runtime.discover_lan(self.settings.timeout):
            parts = resource.split("::")
            if len(parts) < 3 or not parts[0].upper().startswith("TCPIP") or parts[-1].upper() != "INSTR":
                continue
            try:
                addresses = {
                    ipaddress.IPv4Address(row[4][0])
                    for row in socket.getaddrinfo(parts[1], None, socket.AF_INET)
                }
            except (OSError, ValueError):
                continue
            for address in sorted(addresses):
                if not any(address in net for net in networks) or (
                    restriction and not any(address in net for net in restriction)
                ):
                    continue
                actual = "::".join((parts[0], str(address), *parts[2:]))
                found[actual.upper()] = DiscoveryCandidate(
                    ConnectionConfig(actual, timeout=self.settings.timeout),
                    self.name,
                    {
                        "advertisedResource": resource,
                        "address": str(address),
                        "scope": [str(net) for net in networks],
                    },
                    safe_scpi=True,
                )
        return tuple(found.values())


class StaticAddressDiscoveryProvider:
    """Probe only the exact endpoints saved by the operator.

    This provider intentionally does not inspect interfaces, expand subnets,
    or try adjacent ports.  A targeted provider is created for the address
    dialog's one-shot scan while the regular provider is included in every
    normal scan.
    """

    name = "static_addresses"

    def __init__(self, store: StaticAddressStore, ids: tuple[str, ...] | None = None):
        self.store = store
        self.ids = None if ids is None else frozenset(str(value) for value in ids)

    def for_ids(self, ids: tuple[str, ...] | list[str] | set[str]) -> "StaticAddressDiscoveryProvider":
        return type(self)(self.store, tuple(str(value) for value in ids))

    def enumerate(self) -> tuple[DiscoveryCandidate, ...]:
        entries = self.store.entries()
        if self.ids is not None:
            entries = tuple(entry for entry in entries if entry.id in self.ids)
        return tuple(
            DiscoveryCandidate(
                entry.config(),
                self.name,
                {
                    "address": entry.address,
                    "kind": entry.kind,
                    "port": entry.port,
                    "adapter": entry.adapter,
                    "label": entry.label,
                    "static_address_id": entry.id,
                },
                safe_scpi=not bool(entry.adapter),
            )
            for entry in entries
        )
