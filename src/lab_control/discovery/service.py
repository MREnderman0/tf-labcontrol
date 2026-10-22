from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Callable, Protocol
from uuid import uuid4

from lab_control.application.config import ConnectionConfig, canonical_resource
from lab_control.application.leases import ResourceLeases
from lab_control.application.sessions import SessionManager
from lab_control.discovery.addresses import follow_verified_address, network_host, remember_identity
from lab_control.discovery.presence import LIVE_CONNECTIONS, local_enumeration, update_record_presence
from lab_control.domain.instruments import DeviceError, DeviceIdentity, jsonable, utc_now
from lab_control.domain.workspace import GENERAL, WORKSPACE_MODES
from lab_control.storage.repository import Repository
from lab_control.transport.visa import VisaRuntime


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

        def usb_instance_id(port) -> str:
            # USB-serial bridges usually keep this value when Windows assigns a
            # different COM number after reinsertion.  Do not fall back to the
            # port location: a changed socket is exactly the case we want to
            # support.
            if port.serial_number and port.vid is not None and port.pid is not None:
                return f"VID_{port.vid:04X}&PID_{port.pid:04X}\\{port.serial_number}"
            return ""

        return tuple(
            DiscoveryCandidate(
                ConnectionConfig(
                    port.device,
                    transport="serial",
                    usb_instance_id=usb_instance_id(port),
                ),
                self.name,
                {
                    "description": port.description,
                    "vid": port.vid,
                    "pid": port.pid,
                    "usb_serial": port.serial_number,
                    "usb_instance_id": usb_instance_id(port),
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
            if not config.sdk_path:
                from lab_control.application.environment import find_sdk

                config = replace(config, sdk_path=find_sdk(config.transport))
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


class DiscoveryService:
    def __init__(
        self,
        providers: tuple[DiscoveryProvider, ...],
        sessions: SessionManager,
        leases: ResourceLeases,
        inventory: Inventory,
        probe: Callable[[DiscoveryCandidate], tuple[DeviceIdentity, str]],
        *,
        probe_workers: int = 4,
        normalize_discovery_config: Callable[[ConnectionConfig, tuple[str, ...]], ConnectionConfig]
        | None = None,
        source_addresses: Callable[[], tuple[str, ...]] | None = None,
    ) -> None:
        self.providers, self.sessions, self.leases, self.inventory, self.probe = (
            providers,
            sessions,
            leases,
            inventory,
            probe,
        )
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="discovery")
        self.probe_workers = max(1, int(probe_workers))
        self.normalize_discovery_config = normalize_discovery_config
        self.source_addresses = source_addresses
        self.lock, self.cancelled = threading.Lock(), threading.Event()
        self.future: Future[Any] | None = None
        self.progress: dict[str, Any] = {"state": "idle"}
        # Protected by the same lock as scan admission; checked by every frontend.
        self.connection_batch_active = False
        self.closing = False

    @property
    def is_scanning(self) -> bool:
        with self.lock:
            return bool(self.future and not self.future.done())

    def scan(
        self,
        providers: tuple[DiscoveryProvider, ...] | None = None,
        *,
        preserve_presence: bool = False,
        scope: str = "",
    ) -> Future[dict[str, Any]]:
        with self.lock:
            if self.sessions.maintenance_active:
                raise DeviceError("安装向导运行期间暂停扫描", "maintenance_busy")
            if self.closing:
                raise DeviceError("程序正在退出", "runtime_closing")
            if self.connection_batch_active:
                raise DeviceError("正在批量连接，请等待完成或停止连接", "connection_batch_busy")
            if self.future and not self.future.done():
                raise DeviceError("A scan is already running", "scan_busy")
            self.cancelled.clear()
            selected = self.providers if providers is None else tuple(providers)
            self.future = self.worker.submit(self._scan, selected, preserve_presence, scope)
            return self.future

    def cancel(self, scan_id: str = "") -> None:
        self.cancelled.set()

    def _scan(
        self,
        providers: tuple[DiscoveryProvider, ...],
        preserve_presence: bool = False,
        scope: str = "",
    ) -> dict[str, Any]:
        started = time.monotonic()
        scan_id = uuid4().hex
        report: dict[str, Any] = {
            "id": scan_id,
            "startedAt": utc_now(),
            "providers": {},
            "items": [],
            "state": "running",
        }
        if scope:
            report["scope"] = scope
        self.progress = report
        self.inventory.repository.audit("scan_started", scan_id=scan_id)
        successful: set[str] = set()
        observations: dict[str, list[DiscoveryCandidate]] = {}
        with ThreadPoolExecutor(
            max_workers=max(1, len(providers)), thread_name_prefix="enumerate"
        ) as executor:
            futures = {executor.submit(provider.enumerate): provider for provider in providers}
            for future in as_completed(futures):
                provider = futures[future]
                try:
                    result = future.result()
                    successful.add(provider.name)
                    report["providers"][provider.name] = {"count": len(result), "state": "success"}
                    for candidate in result:
                        key = canonical_resource(candidate.config.resource)
                        observations.setdefault(key, []).append(candidate)
                except Exception as exc:
                    report["providers"][provider.name] = {"state": "error", "error": str(exc)}
                self.inventory.repository.audit(
                    "provider_completed",
                    scan_id=scan_id,
                    provider=provider.name,
                    result=report["providers"][provider.name],
                )

        available_sources: tuple[str, ...] = ()
        if self.source_addresses is not None:
            try:
                available_sources = tuple(self.source_addresses())
            except DeviceError:
                # The model normalizer will publish a precise source-address
                # diagnostic for targeted endpoints; discovery remains usable
                # for serial, VISA and SDK candidates.
                available_sources = ()

        # Publish every resource before identity I/O. One slow probe cannot hide the other resources.
        candidates: list[DiscoveryCandidate] = []
        for group in observations.values():
            # Normalize saved model endpoints before the pending inventory row
            # is written.  This keeps the row's connection config identical to
            # the config that will be probed (K+K address-list entries are
            # stored as VISA Socket text but use the official-library
            # transport).  The later normalization below remains necessary
            # after source-address binding has changed a candidate.
            normalized_group = []
            for candidate in group:
                if (
                    self.normalize_discovery_config is not None
                    and self.source_addresses is not None
                    and candidate.source == "static_addresses"
                    and candidate.config.adapter
                ):
                    try:
                        candidate = replace(
                            candidate,
                            config=self.normalize_discovery_config(candidate.config, available_sources),
                        )
                    except DeviceError as exc:
                        candidate = replace(candidate, error=str(exc), error_code=exc.code)
                normalized_group.append(candidate)
            group = tuple(normalized_group)
            for candidate in group:
                self.inventory.merge(candidate, scan_id, reason="pending_probe")
            # Explicit connection settings win, independently of provider completion order.
            chosen = max(group, key=lambda c: (bool(c.config.adapter), c.source == "configured"))
            # The saved address may pin an interface while an older startup
            # config still supplies serial/limits. Keep those identity guards,
            # but use the explicitly selected source for this exact socket.
            bound = next(
                (c for c in group if c.source == "static_addresses" and c.config.source_address), None
            )
            if bound is not None and chosen.config.transport in {"visa", "tcp"}:
                chosen = replace(
                    chosen,
                    config=replace(
                        chosen.config, transport="tcp", source_address=bound.config.source_address
                    ),
                )
            if (
                self.normalize_discovery_config is not None
                and self.source_addresses is not None
                and any(c.source == "static_addresses" for c in group)
                and chosen.config.adapter
            ):
                try:
                    chosen = replace(
                        chosen,
                        config=self.normalize_discovery_config(chosen.config, available_sources),
                    )
                except DeviceError as exc:
                    chosen = replace(chosen, error=str(exc), error_code=exc.code)
            chosen = replace(chosen, safe_scpi=any(c.safe_scpi for c in group))
            observation_error = next((c for c in group if c.error_code), None)
            if observation_error is not None and not chosen.error_code:
                chosen = replace(
                    chosen, error_code=observation_error.error_code, error=observation_error.error
                )
            serial_candidate = next((c for c in group if c.source == "serial"), None)
            if serial_candidate is not None:
                known = self.inventory.by_stable_id(serial_candidate.config.usb_instance_id)
                profile = (
                    ConnectionConfig(**known["config"])
                    if known and known.get("adapterId") and known.get("config")
                    else self.inventory.serial_profile(serial_candidate.descriptor)
                )
                if profile is not None and profile.adapter:
                    # Probe the currently enumerated COM resource with the
                    # confirmed model profile. The USB bridge identity, not
                    # its transient COM number, selects the profile.
                    chosen = replace(
                        serial_candidate,
                        config=replace(
                            profile,
                            resource=serial_candidate.config.resource,
                            transport="serial",
                            aliases=(),
                            usb_instance_id=serial_candidate.config.usb_instance_id,
                        ),
                        safe_scpi=any(c.safe_scpi for c in group),
                    )
            known = self.inventory.by_resource(chosen.config.resource)
            if (
                chosen.safe_scpi
                and network_host(chosen.config.resource)
                and not any(c.source == "configured" for c in group)
                and known
                and known.get("identityKey")
                and not known.get("identityConflict")
                and known["candidates"][chosen.config.resource].get("verifiedIdentityKey")
                == known["identityKey"]
            ):
                # Rediscovery must retain the verified address's connection timeout.
                # Copy no model, serial or limits: DHCP may have reassigned this address.
                chosen = replace(chosen, config=replace(chosen.config, timeout=known["config"]["timeout"]))
            candidates.append(chosen)

        # Each resource is leased atomically before opening a short-lived probe handle.
        def process(candidate: DiscoveryCandidate) -> dict[str, Any]:
            if self.cancelled.is_set():
                return {"resource": candidate.config.resource, "reason": "cancelled"}
            identity, adapter, error, reason = None, candidate.config.adapter, "", "metadata_only"
            error_code = ""
            owner = "probe:" + uuid4().hex
            leased = False
            try:
                if candidate.error_code:
                    raise DeviceError(candidate.error, candidate.error_code)
                with self.sessions.lock:
                    active = self.leases.owner(candidate.config.resource)
                    session = self.sessions.sessions.get(active or "")
                    if session is not None:
                        adapter = session.config.adapter
                        if session.state in LIVE_CONNECTIONS and not session.closing:
                            identity, reason = session.identity, "cached_active_session"
                        else:
                            reason = "cached_session_unconfirmed"
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
                            identified=True,
                        )
                        leased = True
                if leased:
                    identity, adapter = self.probe(candidate)
                    reason = "identified"
            except Exception as exc:
                error, reason = str(exc), "probe_failed"
                error_code = exc.code if isinstance(exc, DeviceError) else ""
            finally:
                if leased:
                    self.leases.release(owner)
            record = self.inventory.merge(
                candidate, scan_id, identity, adapter, error, reason, error_code=error_code
            )
            item = {
                "device_id": record["id"],
                "resource": candidate.config.resource,
                "probeTimeoutSeconds": candidate.config.timeout,
                "reason": reason,
                "error": error,
                "errorCode": error_code,
            }
            self.inventory.repository.audit("discovery_result", scan_id=scan_id, **item)
            return item

        # Identity probes are the expensive part of a startup scan. Bound the
        # number of concurrent probes and keep result ordering deterministic.
        # Mock candidates remain serial because their fixture adapters share a
        # process-global simulation state; real resources are endpoint-locked.
        results: dict[int, dict[str, Any]] = {}
        parallel = [
            (index, candidate)
            for index, candidate in enumerate(candidates)
            if candidate.config.transport != "mock"
        ]
        serial = [
            (index, candidate)
            for index, candidate in enumerate(candidates)
            if candidate.config.transport == "mock"
        ]
        for index, candidate in serial:
            results[index] = process(candidate)
        if parallel:
            with ThreadPoolExecutor(
                max_workers=min(self.probe_workers, len(parallel)), thread_name_prefix="probe"
            ) as probe_executor:
                probe_futures = {
                    probe_executor.submit(process, candidate): index for index, candidate in parallel
                }
                for probe_future in as_completed(probe_futures):
                    results[probe_futures[probe_future]] = probe_future.result()
        report["items"] = [results[index] for index in range(len(candidates))]
        if not preserve_presence:
            self.inventory.stale(scan_id, successful - {"configured"}, cancelled=self.cancelled.is_set())
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
