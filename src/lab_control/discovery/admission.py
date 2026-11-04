"""Read-only evidence for probe admission; never substitutes for a live identity."""

from __future__ import annotations

from dataclasses import dataclass

from lab_control.application.leases import connection_claims
from lab_control.discovery.inventory import Inventory
from lab_control.discovery.types import DiscoveryCandidate
from lab_control.domain.connections import canonical_resource, network_host


@dataclass(frozen=True)
class ProbeClaims:
    resources: tuple[str, ...]
    basis: str = "unconfirmed_resource"
    trusted: bool = False
    conflict: bool = False


def probe_claims(candidate: DiscoveryCandidate, inventory: Inventory) -> ProbeClaims:
    config = candidate.config
    resources = connection_claims(config)
    record = inventory.by_resource(config.resource)
    expected = candidate.descriptor.get("expectedIdentityKey")
    if expected:
        record = next((r for r in inventory.all() if r.get("identityKey") == expected), None)
    if config.usb_instance_id:
        record = inventory.by_stable_id(config.usb_instance_id) or record
    key = record.get("identityKey") if record else None
    if key and record is not None:
        resources += (f"identity:{key}",)
        serial = record.get("identity", {}).get("serial", "")
        if serial and record.get("adapterId"):
            resources += (f"asset:{record['adapterId']}:{serial}",)
    if record and (record.get("identityConflict") or record.get("discoveryState") == "conflict"):
        return ProbeClaims(resources, "identity_conflict", conflict=True)
    if config.adapter and candidate.source in {"configured", "static_addresses"}:
        return ProbeClaims(resources, "explicit_model_resource", trusted=True)
    if expected and record and candidate.source == "dhcp_leases":
        return ProbeClaims(resources, "verified_dhcp_asset", trusted=True)
    if record:
        # An exact bridge binding may select the model's read-only protocol.
        # The bridge ID is a resource claim, not an instrument serial number.
        if (
            config.usb_instance_id
            and record.get("config", {}).get("usb_instance_id") == config.usb_instance_id
            and record.get("identity")
            and config.adapter == record.get("adapterId")
        ):
            return ProbeClaims(resources, "bound_usb_resource", trusted=True)
        primary = record.get("config", {}).get("resource", "")
        entry = record.get("candidates", {}).get(primary, {})
        if (
            key
            and network_host(config.resource)
            and canonical_resource(primary) == canonical_resource(config.resource)
            and entry.get("verifiedIdentityKey") == key
            and not entry.get("retired")
        ):
            return ProbeClaims(resources, "verified_primary_address", trusted=True)
    return ProbeClaims(resources)
