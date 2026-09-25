from __future__ import annotations

import platform
import sys
import threading
from pathlib import Path
from typing import Any

from lab_control.application.config import ConnectionConfig
from lab_control.application.leases import ProcessLease, ResourceLeases
from lab_control.application.sessions import SessionManager
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
from lab_control.instruments.base import identity_from_scpi
from lab_control.instruments.registry import create_adapter, match, transport_for
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
        self.repository = Repository(root if hardware else (root or application_home()) / "simulation")
        self.process_lease: ProcessLease | None = None
        self.closed = False
        self._close_lock = threading.Lock()
        lock_root = None
        try:
            if hardware:
                # Fixed per-user lock even when LAB_CONTROL_HOME/config changes.
                import os

                lock_root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "TF-LabControl"
                self.process_lease = ProcessLease(lock_root, shared=True)
            self.visa = visa or VisaRuntime()
            self.leases = ResourceLeases(lock_root)
            self.inventory = Inventory(self.repository)
            self.sessions = SessionManager(
                self.leases, self.repository, lambda config: create_adapter(config, self.visa)
            )
            self.configurations = configurations
            from lab_control.discovery.lan import (
                LanDiscoveryProvider,
                LanDiscoverySettings,
                load_lan_settings,
            )

            self.lan_settings = (
                load_lan_settings(self.repository.root) if hardware else LanDiscoverySettings()
            )
            providers: tuple[Any, ...] = (ConfiguredDiscoveryProvider(configurations),)
            if hardware:
                providers += (
                    VisaDiscoveryProvider(self.visa),
                    SerialDiscoveryProvider(),
                )
                if self.lan_settings.enabled:
                    providers += (LanDiscoveryProvider(self.visa, self.lan_settings),)
                sdk_groups: dict[tuple[str, str], list[ConnectionConfig]] = {}
                for config in configurations:
                    if config.transport in {"tlpmx", "pico"}:
                        sdk_groups.setdefault((config.transport, config.sdk_path), []).append(config)
                providers += tuple(VendorSdkDiscoveryProvider(tuple(group)) for group in sdk_groups.values())
            self.discovery = DiscoveryService(
                providers, self.sessions, self.leases, self.inventory, self.probe
            )
            from lab_control.application.monitoring import MonitoringService

            self.monitoring = MonitoringService(self.sessions, self.repository)
        except Exception:
            self.repository.close()
            if self.process_lease:
                self.process_lease.close()
            raise

    def probe(self, candidate: DiscoveryCandidate) -> tuple[DeviceIdentity, str]:
        config = candidate.config
        if config.adapter and not candidate.safe_scpi:
            adapter = create_adapter(config, self.visa)
            try:
                return adapter.identify(), config.adapter
            finally:
                adapter.close()
        transport = transport_for(config, self.visa)
        try:
            transport.open()
            identity = identity_from_scpi(transport.query("*IDN?"))
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

    def connect(self, device_id: str, *, configure_acquisition: bool = True):
        from lab_control.domain.instruments import DeviceError

        # Reserve the session under the scan lock, including enumeration and probe gaps.
        with self.discovery.lock:
            if self.discovery.future and not self.discovery.future.done():
                raise DeviceError("正在扫描并核验设备，请等待扫描完成后连接", "scan_busy")
            return self._connect(device_id, configure_acquisition=configure_acquisition)

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

        def save_identity(completed):
            try:
                identity = completed.result()
                self.inventory.merge(
                    DiscoveryCandidate(config, "configured"), "connected", identity, config.adapter
                )
            except Exception as exc:
                self.repository.audit("connection_failed", device_id=device_id, error=str(exc))

        future.add_done_callback(save_identity)
        return future

    def bind_slot(self, device_id: str, slot: str, mode: str) -> None:
        from lab_control.application.catalog import instrument_category, mode_applicable
        from lab_control.domain.instruments import DeviceError

        with self.sessions.lock:
            session = self.sessions.sessions.get(device_id)
            if session and (session.pending or session.state in {"busy", "connecting"}):
                raise DeviceError("Stop the active task before changing slots", "task_busy")
            record = next(item for item in self.inventory.all() if item["id"] == device_id)
            category = instrument_category(record)
            if slot and (not mode_applicable(record, mode) or slot not in {f"{category}:1", f"{category}:2"}):
                raise ValidationError("Slot category or mode is incompatible with this instrument")
            self.inventory.edit(device_id, slot=slot, mode=mode)
            self.repository.audit("slot_changed", device_id=device_id, slot=slot, mode=mode)

    def select_resource(self, device_id: str, resource: str, *, follow_address: bool = False) -> None:
        from lab_control.domain.instruments import DeviceError

        with self.discovery.lock, self.sessions.lock:
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
        errors = []
        try:
            for cleanup in (
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
