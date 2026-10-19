from __future__ import annotations

import platform
import sys
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from lab_control.application.config import ConnectionConfig, canonical_resource
from lab_control.application.leases import ProcessLease, ResourceLeases
from lab_control.application.sessions import SessionManager
from lab_control.application.static_addresses import StaticAddressStore
from lab_control.discovery.service import (
    ConfiguredDiscoveryProvider,
    DiscoveryCandidate,
    DiscoveryService,
    Inventory,
    SerialDiscoveryProvider,
    VendorSdkDiscoveryProvider,
    VisaDiscoveryProvider,
)
from lab_control.domain.instruments import DeviceIdentity, ValidationError
from lab_control.domain.workspace import WORKSPACE_MODES
from lab_control.instruments.base import identity_from_scpi
from lab_control.instruments.registry import (
    create_adapter,
    match,
    normalize_discovery_config,
    transport_for,
)
from lab_control.storage.repository import Repository
from lab_control.transport.visa import VisaRuntime


class ApplicationRuntime:
    def __init__(
        self,
        configurations: tuple[ConnectionConfig, ...] = (),
        *,
        root: Path | None = None,
        hardware: bool = True,
        visa: VisaRuntime | None = None,
    ) -> None:
        from lab_control.storage.repository import application_home

        if not hardware and any(config.transport != "mock" for config in configurations):
            raise ValidationError("Simulation runtime accepts mock transports only")
        if hardware and any(config.transport == "mock" for config in configurations):
            raise ValidationError(
                "Use --mock for simulated configurations; real inventory cannot contain simulation"
            )
        self.hardware = hardware
        # UI choices live for this launch. Scope/spectrum never restore a prior
        # launch's fastest cadence; all pages share the same per-device choices.
        self.monitoring_preferences: dict[str, dict[str, Any]] = {}
        fixed_runtime_home = application_home()
        self.repository = Repository(
            root if hardware else (root or application_home()) / "simulation", simulation=not hardware
        )
        self.process_lease: ProcessLease | None = None
        self.closed = False
        self._close_lock = threading.Lock()
        lock_root = None
        try:
            if hardware:
                # Fixed per-user lock even when LAB_CONTROL_HOME/config changes.
                lock_root = fixed_runtime_home
                # A real runtime is a single process per user.  Opening this
                # compatibility gate shared allowed a GUI and an external
                # PowerShell/CLI launch to enter discovery at the same time;
                # they then collided later on aliases.lock with a misleading
                # identity-probe error.  Keep the process gate exclusive so
                # the second launch fails at the boundary with the holder
                # details, before either process touches an instrument.
                self.process_lease = ProcessLease(lock_root, shared=False)
            self.visa = visa or VisaRuntime()
            self.leases = ResourceLeases(lock_root)
            from lab_control.application.catalog import instrument_category
            from lab_control.storage.workspace_migration import migrate_general_workspace

            self.workspace_migration = migrate_general_workspace(self.repository, instrument_category)
            self.inventory = Inventory(self.repository)
            self.sessions = SessionManager(
                self.leases, self.repository, lambda config: create_adapter(config, self.visa)
            )
            self.configurations = configurations
            from lab_control.discovery.lan import (
                LanDiscoveryProvider,
                LanDiscoverySettings,
                RememberedLanDiscoveryProvider,
                StaticAddressDiscoveryProvider,
                load_lan_settings,
                physical_ipv4_addresses,
            )

            self.lan_settings = (
                load_lan_settings(self.repository.root) if hardware else LanDiscoverySettings()
            )
            self.static_addresses = StaticAddressStore(self.repository)
            self.static_address_provider = StaticAddressDiscoveryProvider(self.static_addresses)
            providers: tuple[Any, ...] = (ConfiguredDiscoveryProvider(configurations),)
            if hardware:
                providers += (
                    VisaDiscoveryProvider(self.visa),
                    SerialDiscoveryProvider(),
                )
                from lab_control.instruments.registry import DEVICE_DEFINITIONS

                providers += tuple(
                    definition.discovery_factory()
                    for definition in DEVICE_DEFINITIONS.values()
                    if definition.discovery_factory is not None
                )
                if self.lan_settings.enabled:
                    providers += (
                        LanDiscoveryProvider(self.visa, self.lan_settings),
                        RememberedLanDiscoveryProvider(self.inventory, self.lan_settings),
                    )
                # Explicitly saved endpoints are safe to query even when
                # service advertisement discovery is disabled. The provider
                # only returns exact operator-entered address/port pairs.
                providers += (self.static_address_provider,)
                sdk_groups: dict[tuple[str, str], list[ConnectionConfig]] = {}
                for config in configurations:
                    if config.transport in {"tlpmx", "pico"}:
                        sdk_groups.setdefault((config.transport, config.sdk_path), []).append(config)
                providers += tuple(VendorSdkDiscoveryProvider(tuple(group)) for group in sdk_groups.values())
            self.discovery = DiscoveryService(
                providers,
                self.sessions,
                self.leases,
                self.inventory,
                self.probe,
                normalize_discovery_config=normalize_discovery_config,
                source_addresses=physical_ipv4_addresses if hardware else None,
            )
            from lab_control.application.monitoring import MonitoringService

            self.monitoring = MonitoringService(self.sessions, self.repository)
            from lab_control.application.bulk_connection import BulkConnectionService

            self.bulk_connection = BulkConnectionService(self)
        except Exception:
            self.repository.close()
            if self.process_lease:
                self.process_lease.close()
            raise

    def probe(self, candidate: DiscoveryCandidate) -> tuple[DeviceIdentity, str]:
        from lab_control.instruments.registry import DEVICE_DEFINITIONS

        config = candidate.config
        definition = DEVICE_DEFINITIONS.get(config.adapter)
        if config.adapter and (
            not candidate.safe_scpi or (definition is not None and definition.identity_requires_adapter)
        ):
            adapter = create_adapter(config, self.visa)
            try:
                return adapter.identify(), config.adapter
            finally:
                adapter.close()
        transport = transport_for(config, self.visa)
        try:
            transport.open()
            identity_probe = None
            if not config.adapter and candidate.safe_scpi and config.resource.upper().startswith("TCPIP"):
                known = self.inventory.by_resource(config.resource)
                if (
                    known
                    and known.get("identityKey")
                    and not known.get("identityConflict")
                    and known.get("discoveryState") != "conflict"
                    and canonical_resource(known["config"]["resource"]) == canonical_resource(config.resource)
                ):
                    entry = known["candidates"].get(known["config"]["resource"], {})
                    if not entry.get("retired") and entry.get("verifiedIdentityKey") == known["identityKey"]:
                        definition = DEVICE_DEFINITIONS.get(known["config"].get("adapter", ""))
                        identity_probe = definition.discovery_identity if definition else None
            # A verified address supplies only its identity handshake. The hook
            # receives this fresh config, without past serial, options or limits.
            identity = (
                identity_probe(transport, config)
                if identity_probe
                else identity_from_scpi(transport.query("*IDN?"))
            )
            return identity, match(identity)
        finally:
            transport.close()

    def add(self, config: ConnectionConfig) -> str:
        if not self.hardware and config.transport != "mock":
            raise ValidationError("Simulation runtime accepts mock transports only")
        if self.hardware and config.transport == "mock":
            raise ValidationError("Mock resources cannot be added to real inventory")
        return self.inventory.merge(
            DiscoveryCandidate(config, "configured"), "manual", adapter=config.adapter
        )["id"]

    def configure_device(self, device_id: str, config: ConnectionConfig) -> None:
        """Apply an explicitly selected adapter to an existing inventory row."""
        from lab_control.domain.instruments import DeviceError

        if not self.hardware and config.transport != "mock":
            raise ValidationError("Simulation runtime accepts mock transports only")
        if self.hardware and config.transport == "mock":
            raise ValidationError("Mock resources cannot be added to real inventory")
        with self.discovery.lock, self.sessions.lock:
            self._check_connection_admission()
            if self.discovery.future and not self.discovery.future.done():
                raise DeviceError("正在扫描并核验设备，请等待扫描完成后配置适配器", "scan_busy")
            session = self.sessions.sessions.get(device_id)
            if session and (session.pending or session.state in {"busy", "connecting"}):
                raise DeviceError("设备正在执行任务，请先停止并断开连接", "task_busy")
            self.inventory.configure(device_id, config)
            self.repository.audit(
                "device_adapter_configured",
                device_id=device_id,
                resource=config.resource,
                adapter=config.adapter,
                transport=config.transport,
            )

    def connect(self, device_id: str, *, configure_acquisition: bool = True):
        from lab_control.domain.instruments import DeviceError

        # Reserve the session under the scan lock, including enumeration and probe gaps.
        with self.discovery.lock:
            self._check_connection_admission()
            if self.discovery.future and not self.discovery.future.done():
                raise DeviceError("正在扫描并核验设备，请等待扫描完成后连接", "scan_busy")
            return self._connect(device_id, configure_acquisition=configure_acquisition)

    def _check_connection_admission(self) -> None:
        from lab_control.domain.instruments import DeviceError

        if self.discovery.closing or self.closed:
            raise DeviceError("程序正在退出", "runtime_closing")
        if self.discovery.connection_batch_active:
            raise DeviceError("正在批量连接，请等待完成或停止连接", "connection_batch_busy")

    def connect_all_discovered(self) -> Future[dict[str, Any]]:
        return self.bulk_connection.start()

    def scan_static_addresses(self, ids: tuple[str, ...] | list[str] | set[str]) -> Future[dict[str, Any]]:
        """Run one scan for selected saved addresses without changing other presence state."""
        from lab_control.domain.instruments import DeviceError

        if not self.hardware:
            raise DeviceError("模拟模式不执行真实地址扫描", "unsupported")
        selected = tuple(str(value) for value in ids)
        if not selected:
            raise ValidationError("至少选择一个静态地址")
        provider = self.static_address_provider.for_ids(selected)
        return self.discovery.scan((provider,), preserve_presence=True, scope="address_list")

    def cancel_connect_all(self) -> None:
        self.bulk_connection.cancel()

    def connection_progress(self) -> dict[str, Any]:
        return self.bulk_connection.snapshot()

    def _connect(self, device_id: str, *, configure_acquisition: bool = True):
        record = next(item for item in self.inventory.all() if item["id"] == device_id)
        if record["discoveryState"] == "conflict":
            from lab_control.domain.instruments import DeviceError

            raise DeviceError("Resolve identity conflict before connection", "identity_conflict")
        from lab_control.discovery.addresses import verified_connection

        config = verified_connection(record)
        from lab_control.application.connection_preferences import qualified_connection

        config, firmware = qualified_connection(record, config, self.repository, self.visa)
        future = self.sessions.connect(
            device_id, config, expected_firmware=firmware, configure_acquisition=configure_acquisition
        )

        saved: Future[DeviceIdentity] = Future()
        # Opening is already submitted; cancelling this publication cannot cancel I/O.
        saved.set_running_or_notify_cancel()

        def save_identity(completed):
            try:
                identity = completed.result()
                self.inventory.merge(
                    DiscoveryCandidate(config, "configured"), "connected", identity, config.adapter
                )
                saved.set_result(identity)
            except Exception as exc:
                try:
                    self.repository.audit("connection_failed", device_id=device_id, error=str(exc))
                finally:
                    saved.set_exception(exc)

        future.add_done_callback(save_identity)
        return saved

    def bind_slot(
        self, device_id: str, slot: str, mode: str, *, replace_device_id: str | None = None
    ) -> None:
        from lab_control.application.catalog import instrument_category, mode_applicable
        from lab_control.domain.instruments import DeviceError

        with self.sessions.lock, self.inventory.lock:
            for key in (device_id, replace_device_id):
                if key is None:
                    continue
                session = self.sessions.sessions.get(key)
                if session and (
                    session.pending
                    or session.state in {"busy", "connecting"}
                    or session.monitoring
                    or session.controls_locked
                    or session.control_pending
                    or session.control_group
                    or session.closing
                ):
                    raise DeviceError("设备正在执行任务，请停止并等待完成后修改槽位", "task_busy")
            record = next(item for item in self.inventory.all() if item["id"] == device_id)
            if mode not in WORKSPACE_MODES:
                raise ValidationError("Unknown workspace mode")
            if slot and (record.get("identityConflict") or record["discoveryState"] == "conflict"):
                raise DeviceError("设备身份冲突，请先核对", "identity_conflict")
            category = instrument_category(record)
            if slot and (not mode_applicable(record, mode) or slot not in {f"{category}:1", f"{category}:2"}):
                raise ValidationError("Slot category or mode is incompatible with this instrument")
            self.inventory.bind_slot(device_id, slot, mode, replace_device_id=replace_device_id)
            self.repository.audit(
                "slot_changed",
                device_id=device_id,
                slot=slot,
                mode=mode,
                replaced_device_id=replace_device_id,
            )

    def select_resource(self, device_id: str, resource: str, *, follow_address: bool = False) -> None:
        from lab_control.domain.instruments import DeviceError

        with self.discovery.lock, self.sessions.lock:
            self._check_connection_admission()
            if self.discovery.future and not self.discovery.future.done():
                raise DeviceError("正在扫描并核验设备，请等待扫描完成后选择连接", "scan_busy")
            if device_id in self.sessions.sessions:
                raise DeviceError("Disconnect before changing the primary resource", "resource_busy")
            self.inventory.select_resource(device_id, resource, follow_address=follow_address)

    def diagnostics(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "python": sys.version,
            "platform": platform.platform(),
            "root": str(self.repository.root),
            "profiles": len(self.configurations),
        }
        try:
            result["visa"] = str(self.visa.manager.visalib)
            library_path = str(self.visa.manager.visalib.library_path)
            result["visa_checks"] = {"path": library_path, "hardware_accepted": False}
            if sys.platform == "win32" and Path(library_path).is_file():
                from lab_control.application.environment import pe_bits

                result["visa_checks"]["bits"] = pe_bits(Path(library_path))
            result["visa_checks"]["functions"] = {
                name: hasattr(self.visa.manager.visalib, name)
                for name in ("viOpenDefaultRM", "viOpen", "viRead", "viWrite", "viClose")
            }
        except Exception as exc:
            result["visa"] = {"error": str(exc), "action": "Install a matching 64-bit VISA runtime"}
        import serial

        result["serial"] = serial.VERSION
        from lab_control.application.environment import dependency_diagnostics

        result["dependencies"] = dependency_diagnostics(self.configurations)
        return result

    def close(self) -> None:
        # GUI shutdown and owner cleanup can overlap while slow reads drain.
        # The second caller must await the same cleanup, not close workers twice.
        with self._close_lock:
            self._close_once()

    def _close_once(self) -> None:
        if self.closed:
            return
        with self.discovery.lock:
            self.discovery.closing = True
        errors = []
        try:
            for cleanup in (
                self.bulk_connection.close,
                self.monitoring.close,
                self.discovery.close,
                self.sessions.close,
                self.visa.close,
            ):
                try:
                    cleanup()
                except Exception as exc:
                    errors.append(exc)
        finally:
            self.repository.close()
            if self.process_lease:
                self.process_lease.close()
            self.closed = True
        if errors:
            raise errors[0]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
