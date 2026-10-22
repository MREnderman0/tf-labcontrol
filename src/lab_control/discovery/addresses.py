"""Verified resource associations are separate from retained address history."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import DeviceError, DeviceIdentity


def network_host(resource: str) -> str:
    if resource.upper().startswith("TCPIP"):
        return resource.split("::")[1].lower()
    if resource.lower().startswith("tcp://"):
        from urllib.parse import urlparse

        return urlparse(resource).hostname or ""
    return ""


def remember_identity(record: dict[str, Any], resource: str, identity: DeviceIdentity, scan_id: str) -> None:
    # These labels are reused across sessions and cannot establish simultaneity.
    session_labels = {"connected", "manual"}
    conflict = record.get("identityConflict", {})
    if (
        conflict.get("reason") == "same_identity_at_multiple_network_hosts"
        and conflict.get("scanRunId") in session_labels
    ):
        # Repair only this legacy false positive after a fresh matching identity read.
        record.pop("identityConflict")
    entry = record["candidates"][resource]
    entry.update(verifiedIdentityKey=identity.key, verifiedScanRunId=scan_id, retired=False)
    host = network_host(resource)
    # Different simultaneous LAN hosts with the same serial require inspection.
    duplicates = [
        other
        for address, other in record["candidates"].items()
        if host
        and scan_id not in session_labels
        and network_host(address)
        and network_host(address) != host
        and other.get("verifiedIdentityKey") == identity.key
        and other.get("verifiedScanRunId") == scan_id
        and not other.get("retired")
    ]
    if duplicates:
        record["identityConflict"] = {
            "reason": "same_identity_at_multiple_network_hosts",
            "scanRunId": scan_id,
        }


def follow_verified_address(
    record: dict[str, Any], resource: str, identity: DeviceIdentity, config: ConnectionConfig
) -> None:
    original = record.get("config", {})
    if not identity.key or record.get("primaryResourceExplicit") or record.get("identityConflict"):
        return
    if original.get("expected_serial") and original["expected_serial"] != identity.serial:
        record["identityConflict"] = {"reason": "configured_serial_differs", "serial": identity.serial}
        return
    if network_host(resource) and network_host(original.get("resource", "")):
        # Retain timeout, limits, options, unit policies and other user settings.
        record["config"] = {
            **original,
            "resource": resource,
            "transport": config.transport,
            "expected_serial": identity.serial,
            "source_address": config.source_address,
        }


def verified_connection(record: dict[str, Any]) -> ConnectionConfig:
    if record.get("identityConflict") or record.get("discoveryState") == "conflict":
        raise DeviceError("Resolve the asset identity conflict before connection", "identity_conflict")
    config = ConnectionConfig(**record["config"])
    candidate = record["candidates"].get(config.resource, {})
    if candidate.get("retired"):
        raise DeviceError(
            "Saved address now belongs to a different device; scan/select a verified address",
            "identity_conflict",
        )
    key = record.get("identityKey")
    if (
        network_host(config.resource)
        and not key
        and (candidate.get("source") == "lan" or "lan" in candidate.get("sources", {}))
    ):
        raise DeviceError(
            "Discovered LAN device has no reliable serial; control is unavailable", "identity_required"
        )
    aliases = tuple(
        address
        for address, entry in record["candidates"].items()
        if address != config.resource
        and key
        and entry.get("verifiedIdentityKey") == key
        and entry.get("verifiedScanRunId") == record.get("scanRunId")
        and entry.get("presenceState") == "present"
        and not entry.get("retired")
    )
    serial = record.get("identity", {}).get("serial", "") if key else ""
    return replace(config, aliases=aliases, expected_serial=config.expected_serial or serial)
