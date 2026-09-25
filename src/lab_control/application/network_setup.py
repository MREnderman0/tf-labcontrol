"""Per-computer lab DHCP planning. Never starts servers or changes the network."""

from __future__ import annotations

import hashlib
import ipaddress
import platform
import sys
from typing import Any

from lab_control.domain.instruments import ValidationError

_PRIVATE_NETWORKS = tuple(
    ipaddress.IPv4Network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def inspect_network(cancel=None) -> dict[str, Any]:
    """Read this computer, with no VISA initialization, probes or DHCP requests."""
    if sys.platform != "win32":
        raise ValidationError("Network deployment inspection currently requires Windows")
    script = r"""
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
$adapters=@(Get-NetAdapter -Physical | ForEach-Object {
    $a=$_
    [pscustomobject]@{
        id=$a.InterfaceGuid.ToString(); name=$a.Name; index=$a.ifIndex; up=($a.Status -eq 'Up')
        ethernet=($a.InterfaceType -eq 6)
        addresses=@(Get-NetIPAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4 |
            Where-Object {$_.AddressState -eq 'Preferred'} | ForEach-Object {
                [pscustomobject]@{ip=$_.IPAddress;prefix=$_.PrefixLength;origin=$_.PrefixOrigin.ToString()}
            })
        gateways=@(Get-NetRoute -InterfaceIndex $a.ifIndex -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
            ForEach-Object {$_.NextHop})
    }
})
[pscustomobject]@{
    machine=(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography').MachineGuid
    adapters=$adapters
    routes=@(Get-NetRoute -AddressFamily IPv4 | Select-Object InterfaceIndex,DestinationPrefix,NextHop,RouteMetric)
    dns=@(Get-DnsClientServerAddress -AddressFamily IPv4 | Select-Object InterfaceIndex,ServerAddresses)
    profiles=@(Get-NetConnectionProfile | Select-Object InterfaceIndex,NetworkCategory)
    dhcp_listeners=@(Get-NetUDPEndpoint -LocalPort 67 -ErrorAction SilentlyContinue |
        Select-Object LocalAddress,OwningProcess)
} | ConvertTo-Json -Depth 6 -Compress
"""
    try:
        from lab_control.deployment.windows import powershell

        snapshot = powershell(script, cancel=cancel, timeout=15)
    except InterruptedError:
        raise
    except (OSError, ValueError, TimeoutError) as exc:
        raise ValidationError("Could not inspect this computer's network; no settings changed") from exc
    machine = snapshot.pop("machine", "")
    if not isinstance(machine, str) or not machine:
        raise ValidationError("Cannot identify this computer; do not reuse another installation's plan")
    snapshot["host_fingerprint"] = hashlib.sha256(machine.encode()).hexdigest()
    snapshot["computer_name"] = platform.node()
    snapshot["read_only"] = True
    return snapshot


def _interface(snapshot: dict[str, Any], selector: str) -> dict[str, Any]:
    candidates = [
        a for a in snapshot["adapters"] if selector.casefold() in {a["id"].casefold(), a["name"].casefold()}
    ]
    if len(candidates) != 1:
        raise ValidationError("Select one current physical Ethernet interface by name or ID")
    adapter = candidates[0]
    if not adapter["up"] or not adapter["ethernet"]:
        raise ValidationError("Lab DHCP requires a connected physical Ethernet interface")
    return adapter


def plan_dhcp(
    snapshot: dict[str, Any],
    interface: str,
    *,
    server_address: str = "",
    pool_start: str = "",
    pool_size: int = 100,
    lease_minutes: int = 1440,
    exclusions: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Prepare a reviewable general pool from current addresses, never a device allowlist."""
    adapter = _interface(snapshot, interface)
    if adapter.get("gateways"):
        raise ValidationError(
            "DHCP server deployment requires a dedicated lab interface without a default gateway"
        )
    if type(pool_size) is not int or not 1 <= pool_size <= 1024:
        raise ValidationError("Pool size must be 1..1024; zero would restrict access to reservations")
    if type(lease_minutes) is not int or not 1 <= lease_minutes <= 10080:
        raise ValidationError("Lease must be 1..10080 minutes")
    addresses = [
        row
        for row in adapter["addresses"]
        if any(ipaddress.IPv4Address(row["ip"]) in net for net in _PRIVATE_NETWORKS)
        and (not server_address or row["ip"] == server_address)
    ]
    if len(addresses) != 1:
        raise ValidationError("Select an existing private IPv4 server address on this Ethernet interface")
    address = addresses[0]
    if address["origin"].casefold() == "dhcp":
        raise ValidationError(
            "This interface already uses DHCP; use that server or configure an isolated lab link"
        )
    network = ipaddress.IPv4Network(f"{address['ip']}/{address['prefix']}", strict=False)
    for other in snapshot["adapters"]:
        if other["id"] == adapter["id"]:
            continue
        for row in other.get("addresses", []):
            if network.overlaps(ipaddress.IPv4Network(f"{row['ip']}/{row['prefix']}", strict=False)):
                raise ValidationError("Lab subnet overlaps another interface; choose an isolated network")
    for route in snapshot.get("routes", []):
        prefix = route["DestinationPrefix"]
        if prefix == "0.0.0.0/0" or route["InterfaceIndex"] == adapter.get("index"):
            continue
        if network.overlaps(ipaddress.IPv4Network(prefix, strict=False)):
            raise ValidationError("Lab subnet overlaps another interface/VPN route")
    if network.prefixlen > 30:
        raise ValidationError("This network has no suitable DHCP client range")
    excluded = {ipaddress.IPv4Address(value) for value in exclusions}
    excluded.update(ipaddress.IPv4Address(row["ip"]) for row in adapter["addresses"])
    if pool_start:
        start = ipaddress.IPv4Address(pool_start)
    else:
        # Find a contiguous range inside this subnet, not a hard-coded site prefix.
        start = network.network_address + 1
        for blocked in sorted(value for value in excluded if value in network):
            if int(start) <= int(blocked) < int(start) + pool_size:
                start = blocked + 1
    end = ipaddress.IPv4Address(int(start) + pool_size - 1)
    if (
        start not in network
        or end not in network
        or start <= network.network_address
        or end >= network.broadcast_address
    ):
        raise ValidationError(
            "Pool must fit wholly inside the selected subnet, excluding network and broadcast"
        )
    if any(start <= value <= end for value in excluded):
        raise ValidationError("Pool overlaps this computer or an explicitly reserved/static address")
    return {
        "schema_version": 1,
        "state": "prepared_only",
        "executor_available": False,
        "host_fingerprint": snapshot["host_fingerprint"],
        "interface_id": adapter["id"],
        "interface_name": adapter["name"],
        "server_address": address["ip"],
        "network": str(network),
        "pool_start": str(start),
        "pool_end": str(end),
        "pool_size": pool_size,
        "mask": str(network.netmask),
        "lease_minutes": lease_minutes,
        "client_policy": "all_dhcp_clients_on_selected_lab_interface",
        "exclusions": sorted(exclusions),
        "dhcp_settings": {
            "IP_Pool": str(start),
            "PoolSize": pool_size,
            "Mask": str(network.netmask),
            "Lease (minutes)": lease_minutes,
            "Gateway": "",
            "DNS": "",
        },
        "required_server_settings": {"DHCP LocalIP": address["ip"], "DHCP Ping": 1},
        "existing_local_dhcp_listeners": snapshot.get("dhcp_listeners", []),
        "interface_gateways": adapter.get("gateways", []),
        "restore_snapshot": {
            "adapter": adapter,
            "routes": snapshot.get("routes", []),
            "dns": snapshot.get("dns", []),
            "profiles": snapshot.get("profiles", []),
        },
        "deployment_steps": [
            "Reinspect host/interface and existing DHCP; reject stale or ambiguous plans",
            "Save exact prior addresses, DHCP state, DNS, routes, services and application-owned firewall rules",
            "Verify trusted offline DHCP payload and acquire deployment ownership",
            "Apply interface-scoped address/pool/service/firewall settings and read them back",
            "Verify actual binding, lease exchange and connectivity; report each result separately",
            "On failure restore owned unchanged settings; preserve shared drivers and user data",
        ],
        "remaining_checks": [
            "Verify this is the intended isolated lab network and whether another DHCP server exists",
            "Check candidate addresses and preserve existing static reservations outside the pool",
            "Install/locate the approved DHCP runtime on this computer and scope its firewall to this interface",
            "Apply settings in the server, verify offer/ack and VISA identity; preparation does not activate DHCP",
        ],
        "migration": "Regenerate on each computer or changed interface/subnet; do not copy leases or firewall paths",
    }


def validate_plan(plan: dict[str, Any], snapshot: dict[str, Any]) -> None:
    """Reject copied or stale deployment plans before any external activation step."""
    if plan.get("schema_version") != 1 or plan.get("state") != "prepared_only":
        raise ValidationError("Unsupported network deployment plan")
    if plan.get("host_fingerprint") != snapshot.get("host_fingerprint"):
        raise ValidationError("Plan belongs to another computer; detect interfaces and prepare again")
    current = plan_dhcp(
        snapshot,
        plan["interface_id"],
        server_address=plan["server_address"],
        pool_start=plan["pool_start"],
        pool_size=plan["pool_size"],
        lease_minutes=plan["lease_minutes"],
        exclusions=tuple(plan["exclusions"]),
    )
    for key in ("network", "mask", "pool_end", "client_policy", "dhcp_settings", "required_server_settings"):
        if plan.get(key) != current[key]:
            raise ValidationError(f"Network or deployment settings changed ({key}); prepare again")
    if "restore_snapshot" in plan and plan["restore_snapshot"] != current["restore_snapshot"]:
        raise ValidationError("Network restore snapshot changed; prepare again")
