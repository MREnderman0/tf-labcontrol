from __future__ import annotations

import threading
from copy import deepcopy
from dataclasses import asdict, replace
from typing import Any
from uuid import uuid4

from lab_control.discovery.addresses import follow_verified_address, network_host, remember_identity
from lab_control.discovery.presence import local_enumeration, update_record_presence
from lab_control.discovery.types import DiscoveryCandidate
from lab_control.domain.connections import ConnectionConfig, canonical_resource
from lab_control.domain.instruments import DeviceError, DeviceIdentity, jsonable, utc_now
from lab_control.domain.workspace import GENERAL, WORKSPACE_MODES
from lab_control.storage.repository import Repository


class Inventory:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository
        self.lock = threading.RLock()
        self.records = {item["id"]: item for item in repository.records()}
        # Persisted evidence describes a previous process, not currently attached hardware.
        # Do not rewrite the user's inventory just for starting the application.
        for record in self.records.values():
            for candidate in record["candidates"].values():
                candidate["presenceState"] = "unknown"
                candidate.pop("evidenceScanRunId", None)
            update_record_presence(record)

    def all(self) -> tuple[dict[str, Any], ...]:
        import copy

        with self.lock:
            return tuple(copy.deepcopy(item) for item in self.records.values())

    def by_resource(self, resource: str) -> dict[str, Any] | None:
        normalized = canonical_resource(resource)
        with self.lock:
            return next(
                (
                    item
                    for item in self.records.values()
                    if any(
                        canonical_resource(res) == normalized
                        and not item["candidates"].get(res, {}).get("retired")
                        for res in item["resources"]
                    )
                ),
                None,
            )

    def by_stable_id(self, usb_instance_id: str) -> dict[str, Any] | None:
        """Find a serial device by its USB bridge identity, independent of COM."""
        if not usb_instance_id:
            return None
        with self.lock:
            matches = [
                item
                for item in self.records.values()
                if item.get("config", {}).get("usb_instance_id") == usb_instance_id
                or any(
                    entry.get("config", {}).get("usb_instance_id") == usb_instance_id
                    or entry.get("rawDescriptor", {}).get("usb_instance_id") == usb_instance_id
                    for entry in item.get("candidates", {}).values()
                )
            ]
            # Prefer an already configured/identified asset when legacy rows
            # and the current stable USB identity coexist in the inventory.
            return max(
                matches,
                key=lambda item: (bool(item.get("adapterId")), bool(item.get("identityKey"))),
                default=None,
            )

    @staticmethod
    def _serial_signature(descriptor: dict[str, Any]) -> tuple[int, int] | None:
        vid, pid = descriptor.get("vid"), descriptor.get("pid")
        return (vid, pid) if isinstance(vid, int) and isinstance(pid, int) else None

    def serial_profile(self, descriptor: dict[str, Any]) -> ConnectionConfig | None:
        """Return an unambiguous configured serial profile for a USB signature.

        This is opt-in through an already configured model. It lets another
        identical bridge be probed after insertion without treating its COM
        number as the device identity.
        """
        signature = self._serial_signature(descriptor)
        if signature is None or not descriptor.get("usb_serial"):
            return None
        matches: dict[tuple[str, str], ConnectionConfig] = {}
        with self.lock:
            for record in self.records.values():
                if not record.get("adapterId"):
                    continue
                try:
                    config = ConnectionConfig(**record["config"])
                except (KeyError, TypeError, ValueError):
                    continue
                if config.transport != "serial":
                    continue
                if any(
                    self._serial_signature(entry.get("rawDescriptor", {})) == signature
                    for entry in record.get("candidates", {}).values()
                ):
                    matches[(config.adapter, config.transport)] = config
        return next(iter(matches.values())) if len(matches) == 1 else None

    def merge(
        self,
        candidate: DiscoveryCandidate,
        scan_id: str,
        identity: DeviceIdentity | None = None,
        adapter: str = "",
        error: str = "",
        reason: str = "",
        *,
        error_code: str = "",
    ) -> dict[str, Any]:
        with self.lock:
            resource = candidate.config.resource
            stable_existing = self.by_stable_id(candidate.config.usb_instance_id)
            resource_existing = self.by_resource(resource)
            existing: dict[str, Any] | None
            # A stable USB identity wins over a reused COM number. If an old
            # record predates this field, adopt it once at the same resource so
            # it can follow the bridge on later insertions.
            if stable_existing is not None:
                existing = stable_existing
            elif candidate.config.usb_instance_id:
                existing = (
                    resource_existing
                    if resource_existing and not resource_existing.get("config", {}).get("usb_instance_id")
                    else None
                )
            elif candidate.config.transport == "serial":
                # Without a USB bridge serial there is no safe asset key. Keep
                # unconfigured metadata together for this endpoint, but do not
                # attach a configured identity to a reused COM number.
                existing = (
                    resource_existing
                    if resource_existing
                    and not resource_existing.get("adapterId")
                    and not resource_existing.get("identityKey")
                    else None
                )
            else:
                existing = resource_existing
            if (
                existing
                and identity
                and identity.key
                and existing.get("identityKey")
                and existing["identityKey"] != identity.key
                and network_host(resource)
                and candidate.safe_scpi
                and reason == "identified"
            ):
                # DHCP reuse does not transfer ownership, limits or historical data.
                existing["candidates"][resource].update(
                    retired=True, presenceState="unreachable", probeReason="address_reassigned"
                )
                update_record_presence(existing)
                self.repository.save(existing)
                existing = None
                candidate = replace(
                    candidate,
                    config=ConnectionConfig(
                        resource,
                        adapter,
                        candidate.config.transport,
                        timeout=candidate.config.timeout,
                        expected_serial=identity.serial,
                        source_address=candidate.config.source_address,
                    ),
                )
            matches = [
                item
                for item in self.records.values()
                if identity and identity.key and item.get("identityKey") == identity.key
            ]
            conflict = bool(
                existing
                and existing.get("identityKey")
                and identity
                and identity.key != existing["identityKey"]
            )
            if len(matches) > 1:
                conflict = True
            if existing and len(matches) == 1 and existing["id"] != matches[0]["id"]:
                if scan_id == "connected":
                    # A just-connected session owns the candidate ID. Retain both until explicit review;
                    # changing its ID here would orphan the worker and callers' handles.
                    conflict = True
                elif not existing.get("identityKey") and not conflict:
                    target = matches[0]
                    if any(
                        mode in target.get("slots", {}) and target["slots"][mode] != slot
                        for mode, slot in existing.get("slots", {}).items()
                    ):
                        conflict = True
                    else:
                        for address in existing["resources"]:
                            if address not in target["resources"]:
                                target["resources"].append(address)
                        target["candidates"].update(existing["candidates"])
                        target["displayName"] = target["displayName"] or existing["displayName"]
                        for mode, slot in existing.get("slots", {}).items():
                            target["slots"].setdefault(mode, slot)
                        self.records.pop(existing["id"])
                        self.repository.remove_inventory(existing["id"])
                        existing = target
            if existing is None and len(matches) == 1:
                existing = matches[0]
            record = existing or {
                "id": uuid4().hex,
                "resources": [],
                "candidates": {},
                "displayName": str(candidate.descriptor.get("display_name", "")),
                "slots": {},
                "identity": {},
                "identityKey": None,
                "adapterId": "",
            }
            if resource not in record["resources"]:
                record["resources"].append(resource)
            if not record["displayName"] and candidate.descriptor.get("display_name"):
                record["displayName"] = str(candidate.descriptor["display_name"])
            previous = record["candidates"].get(resource, {})
            sources = dict(previous.get("sources", {}))
            if not sources and previous.get("source"):
                sources[previous["source"]] = {
                    "scanRunId": previous.get("scanRunId", ""),
                    "observedAt": previous.get("lastSeenAt", ""),
                    "rawDescriptor": previous.get("rawDescriptor", {}),
                }
            now = utc_now()
            sources[candidate.source] = {
                "scanRunId": scan_id,
                "observedAt": now,
                "rawDescriptor": candidate.descriptor,
            }
            entry = {
                **previous,
                "candidateId": previous.get("candidateId", uuid4().hex),
                "scanRunId": scan_id,
                "source": previous.get("source", candidate.source)
                if candidate.source == "configured"
                else candidate.source,
                "sources": sources,
                "rawDescriptor": candidate.descriptor,
                "lastError": error,
                "probeErrorCode": error_code,
                "probeReason": reason,
                "config": asdict(candidate.config),
            }
            local_seen = any(
                observation["scanRunId"] == scan_id and local_enumeration(source, resource)
                for source, observation in sources.items()
            )
            if local_seen and candidate.error_code in {"driver_missing", "driver_error"}:
                entry.update(presenceState="present", evidenceScanRunId=scan_id)
            elif error or reason == "probe_failed":
                entry.update(presenceState="unreachable", evidenceScanRunId=scan_id)
            elif identity is not None or local_seen:
                entry.update(presenceState="present", evidenceScanRunId=scan_id)
            elif reason != "pending_probe" and scan_id != "manual":
                entry.update(presenceState="unknown", evidenceScanRunId=scan_id)
            else:
                entry.setdefault("presenceState", "unknown")
            # A configured address alone (including failed probing) is not a sighting.
            if not error and (identity is not None or local_enumeration(candidate.source, resource)):
                entry["lastSeenAt"] = now
                record["lastSeenAt"] = now
            record["candidates"][resource] = entry
            if (
                candidate.source == "serial"
                and candidate.config.usb_instance_id
                and record.get("adapterId")
                and record.get("config")
                and not record.get("identityConflict")
            ):
                # The adapter profile follows the USB bridge; only the current
                # COM endpoint is refreshed for this scan.
                profile = ConnectionConfig(**record["config"])
                record["config"] = asdict(
                    replace(
                        profile,
                        resource=resource,
                        transport="serial",
                        aliases=(),
                        usb_instance_id=candidate.config.usb_instance_id,
                    )
                )
                record["primaryResourceExplicit"] = False
            prefer_visa = bool(
                identity is not None
                and not conflict
                and scan_id != "connected"
                and candidate.config.transport == "visa"
                and record.get("config", {}).get("transport") != "visa"
                and not record.get("primaryResourceExplicit")
                and "config" in record
            )
            if (
                "config" not in record
                or (candidate.config.adapter and not record["config"].get("adapter"))
                or scan_id == "manual"
                or prefer_visa
            ):
                record["config"] = (
                    {**record["config"], "resource": resource, "transport": "visa", "sdk_path": ""}
                    if prefer_visa
                    else asdict(candidate.config)
                )
            if identity and not conflict:
                record["identity"], record["identityKey"] = jsonable(identity), identity.key
                if identity.key:
                    remember_identity(record, resource, identity, scan_id)
                if reason == "identified":
                    follow_verified_address(record, resource, identity, candidate.config)
                    if (
                        candidate.source == "dhcp_leases"
                        and candidate.descriptor.get("expectedIdentityKey") == identity.key
                        and candidate.descriptor.get("dhcpMac")
                    ):
                        record["dhcpBinding"] = {
                            "mac": candidate.descriptor["dhcpMac"],
                            "identityKey": identity.key,
                        }
                if reason != "cached_active_session":
                    record["lastProbeAt"] = now
            if adapter:
                record["adapterId"] = adapter
                record["config"]["adapter"] = adapter
            record.update(
                {
                    "discoveryState": "conflict"
                    if conflict or record.get("identityConflict")
                    else "identified"
                    if record["identity"]
                    else "unknown",
                    "scanRunId": scan_id,
                    "lastError": error,
                    "probeReason": reason,
                    "probeErrorCode": error_code,
                    "driverStatus": "error"
                    if error_code in {"driver_missing", "driver_error"}
                    else "unconfirmed"
                    if error
                    else "enumerated",
                }
            )
            if conflict:
                record["identityConflict"] = jsonable(identity)
            update_record_presence(record)
            self.records[record["id"]] = record
            self.repository.save(record)
            return record

    def edit(
        self,
        device_id: str,
        *,
        display_name: str | None = None,
        slot: str | None = None,
        mode: str = GENERAL,
    ) -> None:
        if mode not in WORKSPACE_MODES:
            raise DeviceError("Unknown workspace mode", "invalid_parameter")
        with self.lock:
            record = deepcopy(self.records[device_id])
            if display_name is not None:
                record["displayName"] = display_name.strip()[:120]
            if slot is not None:
                for other in self.records.values():
                    if slot and other["id"] != device_id and other.get("slots", {}).get(mode) == slot:
                        raise DeviceError("Slot is already occupied", "slot_conflict")
                record["slots"][mode] = slot
                if mode == GENERAL:
                    record.pop("slotMigrationConflict", None)
            self.repository.save(record)
            self.records[device_id] = record

    def configure(self, device_id: str, config: ConnectionConfig) -> None:
        """Attach an explicit adapter to one discovered resource.

        Serial enumeration publishes metadata for every port.  A confirmed
        serial profile may later follow matching USB bridge signatures, while a
        port without a stable USB identity remains metadata-only.
        """
        with self.lock:
            if device_id not in self.records:
                raise DeviceError("设备记录不存在，请重新扫描", "device_not_found")
            record = deepcopy(self.records[device_id])
            normalized = canonical_resource(config.resource)
            for other in self.records.values():
                if other["id"] == device_id:
                    continue
                same_resource = any(
                    canonical_resource(resource) == normalized
                    and not other["candidates"].get(resource, {}).get("retired")
                    for resource in other["resources"]
                )
                if same_resource and (other.get("adapterId") or other.get("identityKey")):
                    raise DeviceError("该连接资源已属于另一条设备记录，请先选择主资源", "resource_conflict")
            resource = config.resource
            entry = record["candidates"].get(resource)
            if config.transport == "serial" and not config.usb_instance_id and entry:
                stable_id = entry.get("rawDescriptor", {}).get("usb_instance_id", "")
                if stable_id:
                    config = replace(config, usb_instance_id=stable_id)
            if resource not in record["resources"]:
                record["resources"].append(resource)
            if entry is None:
                entry = {
                    "candidateId": uuid4().hex,
                    "source": "configured",
                    "sources": {},
                    "rawDescriptor": {},
                    "presenceState": "unknown",
                }
            sources = dict(entry.get("sources", {}))
            sources["configured"] = {
                "scanRunId": "manual",
                "observedAt": utc_now(),
                "rawDescriptor": entry.get("rawDescriptor", {}),
            }
            entry.update(
                config=asdict(config),
                source="configured",
                sources=sources,
                probeReason="manual_configured",
                lastError="",
                probeErrorCode="",
                retired=False,
            )
            record["candidates"][resource] = entry
            record["config"] = asdict(config)
            record["adapterId"] = config.adapter
            record["discoveryState"] = "identified" if record.get("identity") else "unknown"
            record["probeReason"] = "manual_configured"
            record["lastError"] = ""
            record["probeErrorCode"] = ""
            record["driverStatus"] = "unconfirmed"
            # A serial COM number is a current endpoint, not an asset address.
            # The next scan may replace it when the same USB bridge reappears.
            record["primaryResourceExplicit"] = config.transport != "serial"
            update_record_presence(record)
            self.repository.save(record)
            self.records[device_id] = record

    def bind_slot(
        self, device_id: str, slot: str, mode: str, *, replace_device_id: str | None = None
    ) -> None:
        """Caller holds the session lock while validating both participants."""
        if mode not in WORKSPACE_MODES:
            raise DeviceError("Unknown workspace mode", "invalid_parameter")
        with self.lock:
            owners = [
                item
                for item in self.records.values()
                if slot and item["id"] != device_id and item.get("slots", {}).get(mode) == slot
            ]
            if replace_device_id is not None:
                if [item["id"] for item in owners] != [replace_device_id]:
                    raise DeviceError("槽位占用已变化，请重新选择并确认", "slot_changed")
            elif owners:
                raise DeviceError("该槽位已有设备绑定，请确认是否替换", "slot_conflict")
            record = deepcopy(self.records[device_id])
            record.setdefault("slots", {})[mode] = slot
            if mode == GENERAL:
                record.pop("slotMigrationConflict", None)
            changed = [record]
            for owner in owners:
                previous = deepcopy(owner)
                previous["slots"][mode] = ""
                changed.append(previous)
            self.repository.save_many(tuple(changed))
            self.records.update((item["id"], item) for item in changed)

    def select_resource(self, device_id: str, resource: str, *, follow_address: bool = False) -> None:
        with self.lock:
            record = self.records[device_id]
            if resource not in record["candidates"]:
                raise DeviceError("Resource does not belong to this inventory record", "invalid_parameter")
            entry = record["candidates"][resource]
            if entry.get("retired"):
                raise DeviceError("Address was reassigned to another device", "identity_conflict")
            serial = record.get("identity", {}).get("serial", "")
            if follow_address:
                if not network_host(resource):
                    raise DeviceError("Address following requires a LAN resource", "invalid_parameter")
                if record.get("identityConflict") or record.get("discoveryState") == "conflict":
                    raise DeviceError("Resolve the asset identity conflict first", "identity_conflict")
                if (
                    not serial
                    or not record.get("identityKey")
                    or entry.get("verifiedIdentityKey") != record["identityKey"]
                ):
                    raise DeviceError("Verify this LAN resource and serial first", "identity_required")
                expected = record["config"].get("expected_serial")
                if expected and expected != serial:
                    raise DeviceError("Configured serial differs from asset identity", "identity_conflict")
            config = {
                **record["config"],
                "resource": resource,
                "transport": entry["config"]["transport"],
                "adapter": record["adapterId"],
            }
            if follow_address:
                config["expected_serial"] = serial
            record["config"] = config
            record["primaryResourceExplicit"] = not follow_address
            self.repository.save(record)

    def stale(self, scan_id: str, successful_sources: set[str], *, cancelled: bool = False) -> None:
        with self.lock:
            for record in self.records.values():
                for resource, candidate in record["candidates"].items():
                    if candidate.get("evidenceScanRunId") == scan_id:
                        continue
                    if any(
                        observation.get("scanRunId") == scan_id and local_enumeration(source, resource)
                        for source, observation in candidate.get("sources", {}).items()
                    ):
                        # A concurrent connection may have replaced the evidence ID.
                        # Do not discard this scan's physical sighting during finalization.
                        continue
                    sources = set(candidate.get("sources", {})) or {candidate.get("source", "")}
                    physical = {source for source in sources if local_enumeration(source, resource)}
                    missing = bool(physical and physical <= successful_sources and not cancelled)
                    candidate["presenceState"] = "not_seen" if missing else "unknown"
                    candidate["probeReason"] = (
                        "not_seen"
                        if missing
                        else "scan_cancelled"
                        if cancelled
                        else "enumeration_unconfirmed"
                    )
                update_record_presence(record)
                self.repository.save(record)
