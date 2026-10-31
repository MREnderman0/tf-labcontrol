"""Read local DHCP leases as address hints for already verified LAN assets."""

from __future__ import annotations

import configparser
import ipaddress
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lab_control.discovery.inventory import Inventory

from lab_control.discovery.lan import LanDiscoverySettings, discovery_networks
from lab_control.discovery.types import DiscoveryCandidate
from lab_control.domain.connections import ConnectionConfig, network_host
from lab_control.domain.instruments import DeviceError


@dataclass(frozen=True)
class DhcpLease:
    mac: str
    address: str


def normalize_mac(value: str) -> str:
    compact = value.replace(":", "").replace("-", "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{12}", compact):
        raise ValueError("Invalid DHCP hardware address")
    raw = bytes.fromhex(compact)
    if raw[0] & 1 or not any(raw):
        raise ValueError("DHCP hardware address must be unicast")
    return raw.hex(":").upper()


def tftpd_config_path() -> Path | None:
    """Resolve the installed service configuration without starting or changing it."""
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\Tftpd32_svc"
        ) as key:
            try:
                environment = winreg.QueryValueEx(key, "Environment")[0]
            except FileNotFoundError:
                environment = []
            for item in environment if isinstance(environment, list) else [environment]:
                name, separator, value = item.partition("=")
                if separator and name.upper() == "TFTP_INI":
                    path = Path(os.path.expandvars(value.strip('"')))
                    if not path.is_absolute():
                        raise DeviceError("DHCP service INI path is not absolute", "configuration_invalid")
                    return path
            command = winreg.QueryValueEx(key, "ImagePath")[0]
    except FileNotFoundError:
        return None
    match = re.match(r'^\s*"([^"]+\.exe)"|^\s*(.+?\.exe)(?:\s|$)', command, re.I)
    if not match:
        raise DeviceError("Cannot resolve the DHCP service executable", "configuration_invalid")
    executable = Path(os.path.expandvars(match[1] or match[2]))
    if not executable.is_absolute():
        raise DeviceError("DHCP service executable is not absolute", "configuration_invalid")
    return executable.with_name("tftpd32.ini")


def read_tftpd_leases(path: Path, *, now: datetime | None = None) -> tuple[DhcpLease, ...]:
    """Ignore offers, expired/ambiguous entries and invalid leases; never infer presence."""
    if path.stat().st_size > 1024 * 1024:
        raise DeviceError("DHCP lease file exceeds the read limit", "configuration_invalid")
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(path.read_text(encoding="utf-8-sig"))
    if not parser.has_section("DHCP"):
        return ()
    section = parser["DHCP"]
    count = section.getint("Lease_NumLeases", fallback=0)
    minutes = section.getint("Lease (minutes)", fallback=0)
    if not 0 <= count <= 4096 or not 0 < minutes <= 525600:
        raise DeviceError("Invalid DHCP lease count or lifetime", "configuration_invalid")
    current = now or datetime.now()
    leases = []
    for index in range(count):
        prefix = f"Lease_{index}_"
        try:
            mac = normalize_mac(section[prefix + "MAC"])
            address = ipaddress.IPv4Address(section[prefix + "IP"])
            started = datetime.strptime(section[prefix + "LeaseStartTime"], "%m/%d/%Y/%H:%M:%S")
        except (KeyError, ValueError):
            continue
        if (
            address.is_unspecified
            or address.is_loopback
            or address.is_multicast
            or address == ipaddress.IPv4Address("255.255.255.255")
            or not started <= current < started + timedelta(minutes=minutes)
        ):
            continue
        leases.append(DhcpLease(mac, str(address)))
    # A conflicting MAC/IP is not a reason to choose an arbitrary client.
    mac_counts = Counter(lease.mac for lease in leases)
    address_counts = Counter(lease.address for lease in leases)
    return tuple(lease for lease in leases if mac_counts[lease.mac] == address_counts[lease.address] == 1)


def local_dhcp_leases() -> tuple[DhcpLease, ...]:
    path = tftpd_config_path()
    return read_tftpd_leases(path) if path is not None else ()


class DhcpLeaseDiscoveryProvider:
    """Follow a verified asset's MAC to a fresh lease, then require its real identity.

    First association uses the verified current address and is committed only
    after another successful identity query. Ports come from verified resources.
    Unknown clients never receive guessed SCPI commands or port sweeps.
    """

    name = "dhcp_leases"

    def __init__(self, inventory: Inventory, settings: LanDiscoverySettings):
        self.inventory, self.settings = inventory, settings

    def enumerate(self) -> tuple[DiscoveryCandidate, ...]:
        from lab_control.instruments.registry import DEVICE_DEFINITIONS

        if not self.settings.enabled:
            return ()
        networks, restriction = discovery_networks(self.settings)
        if not networks:
            return ()
        leases = local_dhcp_leases()
        candidates = []
        for record in self.inventory.all():
            config = record.get("config", {})
            resource = config.get("resource", "")
            parts = resource.split("::")
            host = network_host(resource)
            definition = DEVICE_DEFINITIONS.get(config.get("adapter", ""))
            model_config = definition.discovery_lan_config if definition is not None else None
            visa_resource = (
                config.get("transport") == "visa"
                and len(parts) in (3, 4)
                and bool(re.fullmatch(r"TCPIP\d*", parts[0], re.I))
                and parts[-1].upper() in {"INSTR", "SOCKET"}
                and (parts[-1].upper() != "SOCKET" or (len(parts) == 4 and parts[2].isdigit()))
            )
            entry = record.get("candidates", {}).get(resource, {})
            identity_key = record.get("identityKey")
            if (
                not identity_key
                or not record.get("identity", {}).get("serial")
                or record.get("primaryResourceExplicit")
                or record.get("identityConflict")
                or record.get("discoveryState") == "conflict"
                or not config.get("adapter")
                or not host
                or not (visa_resource or model_config)
            ):
                continue
            binding = record.get("dhcpBinding", {})
            if binding.get("identityKey") == identity_key:
                matched = [lease for lease in leases if lease.mac == binding.get("mac")]
            elif entry.get("verifiedIdentityKey") == identity_key and not entry.get("retired"):
                matched = [lease for lease in leases if lease.address == host]
            else:
                continue
            if len(matched) != 1:
                continue
            lease = matched[0]
            address = ipaddress.IPv4Address(lease.address)
            if not any(
                address in net and address not in (net.network_address, net.broadcast_address)
                for net in networks
            ) or (restriction and not any(address in net for net in restriction)):
                continue
            timeout = config.get("timeout", self.settings.timeout)
            if model_config:
                probe_config = model_config(
                    ConnectionConfig(resource, config["adapter"], config["transport"], timeout=timeout),
                    lease.address,
                )
            else:
                actual = "::".join((parts[0], lease.address, *parts[2:]))
                probe_config = ConnectionConfig(actual, timeout=timeout)
            candidates.append(
                DiscoveryCandidate(
                    probe_config,
                    self.name,
                    {
                        "dhcpMac": lease.mac,
                        "expectedIdentityKey": identity_key,
                        "scope": [str(net) for net in networks],
                    },
                    safe_scpi=not bool(model_config),
                )
            )
        return tuple(candidates)
