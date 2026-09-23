from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Protocol
from uuid import uuid4

from lab_control.application.config import ConnectionConfig, canonical_resource
from lab_control.application.leases import ResourceLeases
from lab_control.application.sessions import SessionManager
from lab_control.domain.instruments import DeviceError, DeviceIdentity, jsonable, utc_now
from lab_control.storage.repository import Repository
from lab_control.transport.visa import VisaRuntime


@dataclass(frozen=True)
class DiscoveryCandidate:
    config: ConnectionConfig
    source: str
    descriptor: dict[str, Any] = field(default_factory=dict)
    safe_scpi: bool = False


class DiscoveryProvider(Protocol):
    name: str

    def enumerate(self) -> tuple[DiscoveryCandidate, ...]: ...


class VisaDiscoveryProvider:
    name = "visa"

    def __init__(self, runtime: VisaRuntime) -> None:
        self.runtime = runtime

    def enumerate(self) -> tuple[DiscoveryCandidate, ...]:
        # USB instrument manufacturers with documented IEEE 488.2 identity support.
        allowlist = {0x1AB1, 0xF4EC, 0x0699, 0x0AAD, 0x0957, 0x2A8D, 0x05FF}
        candidates = []
        for resource in self.runtime.enumerate():
            parts = resource.split("::")
            safe = False
            if resource.upper().startswith("USB") and resource.upper().endswith("::INSTR"):
                try:
                    safe = int(parts[1], 0) in allowlist
                except (ValueError, IndexError):
                    pass
            candidates.append(
                DiscoveryCandidate(ConnectionConfig(resource), self.name, {"visa_resource": resource}, safe)
            )
        return tuple(candidates)


class SerialDiscoveryProvider:
    name = "serial"

    def enumerate(self) -> tuple[DiscoveryCandidate, ...]:
        from serial.tools.list_ports import comports

        return tuple(
            DiscoveryCandidate(
                ConnectionConfig(port.device, transport="serial"),
                self.name,
                {
                    "description": port.description,
                    "vid": port.vid,
                    "pid": port.pid,
                    "usb_serial": port.serial_number,
                    "location": port.location,
                },
            )
            for port in comports()
        )


class VendorSdkDiscoveryProvider:
    name = "sdk"

    def __init__(self, configurations: tuple[ConnectionConfig, ...]) -> None:
        self.configurations = configurations
        if configurations:
            self.name = (
                "sdk:"
                + configurations[0].transport
                + ":"
                + hashlib.sha256(configurations[0].sdk_path.encode()).hexdigest()[:8]
            )

    def enumerate(self) -> tuple[DiscoveryCandidate, ...]:
        from lab_control.instruments.optical import enumerate_tlpmx
        from lab_control.instruments.pico import enumerate_pico

        result = []
        seen = set()
        for config in self.configurations:
            key = config.transport, config.sdk_path
            if key in seen:
                continue
            seen.add(key)
            enumerate_resources = {"tlpmx": enumerate_tlpmx, "pico": enumerate_pico}.get(config.transport)
            if enumerate_resources:
                for resource in enumerate_resources(config):
                    result.append(
                        DiscoveryCandidate(
                            config
                            if canonical_resource(config.resource) == canonical_resource(resource)
                            else ConnectionConfig(
                                resource,
                                config.adapter,
                                config.transport,
                                timeout=config.timeout,
                                sdk_path=config.sdk_path,
                            ),
                            self.name,
                            {"enumeration": "vendor SDK metadata; no open"},
                        )
                    )
        return tuple(result)


class ConfiguredDiscoveryProvider:
    name = "configured"

    def __init__(self, configurations: tuple[ConnectionConfig, ...]) -> None:
        self.configurations = configurations

    def enumerate(self) -> tuple[DiscoveryCandidate, ...]:
        return tuple(
            DiscoveryCandidate(config, self.name, {"configured": True}) for config in self.configurations
        )


class Inventory:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository
        self.lock = threading.RLock()
        self.records = {item["id"]: item for item in repository.records()}

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
                    if any(canonical_resource(res) == normalized for res in item["resources"])
                ),
                None,
            )

    def merge(
        self,
        candidate: DiscoveryCandidate,
        scan_id: str,
        identity: DeviceIdentity | None = None,
        adapter: str = "",
        error: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        with self.lock:
            resource = candidate.config.resource
            existing = self.by_resource(resource)
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
                        target.get("slots", {}).get(mode) not in (None, "", slot)
                        for mode, slot in existing.get("slots", {}).items()
                    ):
                        conflict = True
                    else:
                        for address in existing["resources"]:
                            if address not in target["resources"]:
                                target["resources"].append(address)
                        target["candidates"].update(existing["candidates"])
                        target["displayName"] = target["displayName"] or existing["displayName"]
                        target["slots"].update(existing.get("slots", {}))
                        self.records.pop(existing["id"])
                        self.repository.remove_inventory(existing["id"])
                        existing = target
            if existing is None and len(matches) == 1:
                existing = matches[0]
            record = existing or {
                "id": uuid4().hex,
                "resources": [],
                "candidates": {},
                "displayName": "",
                "slots": {},
                "identity": {},
                "identityKey": None,
                "adapterId": "",
            }
            if resource not in record["resources"]:
                record["resources"].append(resource)
            record["candidates"][resource] = {
                "candidateId": record["candidates"].get(resource, {}).get("candidateId", uuid4().hex),
                "scanRunId": scan_id,
                "source": candidate.source,
                "lastSeenAt": utc_now(),
                "rawDescriptor": candidate.descriptor,
                "lastError": error,
                "probeReason": reason,
                "config": asdict(candidate.config),
            }
            if (
                "config" not in record
                or (candidate.config.adapter and not record["config"].get("adapter"))
                or scan_id == "manual"
            ):
                record["config"] = asdict(candidate.config)
            if identity and not conflict:
                record["identity"], record["identityKey"] = jsonable(identity), identity.key
                record["lastProbeAt"] = utc_now()
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
                    "lastSeenAt": utc_now(),
                    "scanRunId": scan_id,
                    "lastError": error,
                    "probeReason": reason,
                    "driverStatus": "error" if error else "enumerated",
                }
            )
            if conflict:
                record["identityConflict"] = jsonable(identity)
            self.records[record["id"]] = record
            self.repository.save(record)
            return record

    def edit(
        self,
        device_id: str,
        *,
        display_name: str | None = None,
        slot: str | None = None,
        mode: str = "electronic",
    ) -> None:
        with self.lock:
            record = self.records[device_id]
            if display_name is not None:
                record["displayName"] = display_name.strip()[:120]
            if slot is not None:
                for other in self.records.values():
                    if slot and other["id"] != device_id and other.get("slots", {}).get(mode) == slot:
                        raise DeviceError("Slot is already occupied", "slot_conflict")
                record["slots"][mode] = slot
            self.repository.save(record)

    def select_resource(self, device_id: str, resource: str) -> None:
        with self.lock:
            record = self.records[device_id]
            if resource not in record["candidates"]:
                raise DeviceError("Resource does not belong to this inventory record", "invalid_parameter")
            config = dict(record["candidates"][resource]["config"])
            config["adapter"] = record["adapterId"]
            record["config"] = config
            self.repository.save(record)

    def stale(self, scan_id: str, successful_sources: set[str]) -> None:
        with self.lock:
            for record in self.records.values():
                candidates = record["candidates"].values()
                if candidates and all(
                    c["source"] in successful_sources and c["scanRunId"] != scan_id for c in candidates
                ):
                    record["discoveryState"] = "stale"
                    self.repository.save(record)


class DiscoveryService:
    def __init__(
        self,
        providers: tuple[DiscoveryProvider, ...],
        sessions: SessionManager,
        leases: ResourceLeases,
        inventory: Inventory,
        probe: Callable[[DiscoveryCandidate], tuple[DeviceIdentity, str]],
    ) -> None:
        self.providers, self.sessions, self.leases, self.inventory, self.probe = (
            providers,
            sessions,
            leases,
            inventory,
            probe,
        )
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="discovery")
        self.lock, self.cancelled = threading.Lock(), threading.Event()
        self.future: Future[Any] | None = None
        self.progress: dict[str, Any] = {"state": "idle"}

    def scan(self) -> Future[dict[str, Any]]:
        with self.lock:
            if self.future and not self.future.done():
                raise DeviceError("A scan is already running", "scan_busy")
            self.cancelled.clear()
            self.future = self.worker.submit(self._scan)
            return self.future

    def cancel(self, scan_id: str = "") -> None:
        self.cancelled.set()

    def _scan(self) -> dict[str, Any]:
        started = time.monotonic()
        scan_id = uuid4().hex
        report: dict[str, Any] = {
            "id": scan_id,
            "startedAt": utc_now(),
            "providers": {},
            "items": [],
            "state": "running",
        }
        self.progress = report
        self.inventory.repository.audit("scan_started", scan_id=scan_id)
        successful: set[str] = set()
        candidates: dict[str, DiscoveryCandidate] = {}
        with ThreadPoolExecutor(
            max_workers=max(1, len(self.providers)), thread_name_prefix="enumerate"
        ) as executor:
            futures = {executor.submit(provider.enumerate): provider for provider in self.providers}
            for future in as_completed(futures):
                provider = futures[future]
                try:
                    result = future.result()
                    successful.add(provider.name)
                    report["providers"][provider.name] = {"count": len(result), "state": "success"}
                    for candidate in result:
                        key = canonical_resource(candidate.config.resource)
                        if key not in candidates or candidate.config.adapter:
                            candidates[key] = candidate
                except Exception as exc:
                    report["providers"][provider.name] = {"state": "error", "error": str(exc)}
                self.inventory.repository.audit(
                    "provider_completed",
                    scan_id=scan_id,
                    provider=provider.name,
                    result=report["providers"][provider.name],
                )

        # Publish every resource before identity I/O. One slow probe cannot hide the other resources.
        for candidate in candidates.values():
            self.inventory.merge(candidate, scan_id, reason="pending_probe")

        # Each resource is leased atomically before opening a short-lived probe handle.
        def process(candidate: DiscoveryCandidate) -> dict[str, Any]:
            if self.cancelled.is_set():
                return {"resource": candidate.config.resource, "reason": "cancelled"}
            identity, adapter, error, reason = None, candidate.config.adapter, "", "metadata_only"
            owner = "probe:" + uuid4().hex
            leased = False
            try:
                with self.sessions.lock:
                    active = self.leases.owner(candidate.config.resource)
                    session = self.sessions.sessions.get(active or "")
                    if session is not None:
                        identity, adapter, reason = (
                            session.identity,
                            session.config.adapter,
                            "cached_active_session",
                        )
                    elif self.sessions.sessions:
                        reason = "deferred_possible_alias_of_active_device"
                    elif candidate.config.adapter and candidate.config.transport in {
                        "pico",
                        "tlpmx",
                        "pm100d",
                        "tserve",
                    }:
                        reason = "sdk_identity_requires_explicit_connection"
                    elif candidate.config.adapter or candidate.safe_scpi:
                        self.leases.acquire(
                            owner,
                            (candidate.config.resource, *candidate.config.aliases, "discovery:unknown_alias"),
                        )
                        leased = True
                if leased:
                    identity, adapter = self.probe(candidate)
                    reason = "identified"
            except Exception as exc:
                error, reason = str(exc), "probe_failed"
            finally:
                if leased:
                    self.leases.release(owner)
            record = self.inventory.merge(candidate, scan_id, identity, adapter, error, reason)
            item = {
                "device_id": record["id"],
                "resource": candidate.config.resource,
                "reason": reason,
                "error": error,
            }
            self.inventory.repository.audit("discovery_result", scan_id=scan_id, **item)
            return item

        # Serial probing avoids colliding unconfirmed USB/LAN aliases during the same scan.
        for candidate in candidates.values():
            report["items"].append(process(candidate))
        if not self.cancelled.is_set():
            self.inventory.stale(scan_id, successful - {"configured"})
        report.update(
            {
                "state": "cancelled" if self.cancelled.is_set() else "complete",
                "finishedAt": utc_now(),
                "duration": time.monotonic() - started,
            }
        )
        self.inventory.repository.scan(report)
        self.inventory.repository.audit("scan_finished", **report)
        return report

    def close(self) -> None:
        self.cancel()
        self.worker.shutdown(wait=True)
