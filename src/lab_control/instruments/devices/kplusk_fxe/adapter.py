"""K+K FXE adapter backed by the official K+K Library.

The vendor library owns the wire protocol and its connection lifecycle.  The
compatibility layer is read-only: binary frequency reports are decoded for
monitoring, while no K+K configuration command is sent.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import (
    AcquisitionRequest,
    ApplyResult,
    CapabilitySet,
    DeviceCommand,
    DeviceError,
    DeviceIdentity,
    DeviceStatus,
    Measurement,
    MeasurementBatch,
    ParameterReading,
    Unsupported,
)
from lab_control.instruments.base import ScpiAdapter

from .profile import capability_schema, profile
from .protocol import FrequencyReport, parse_frequency_report, parse_phase_report, validate_channels


class _NoopTransport:
    """Satisfy the shared adapter base without creating a byte transport."""

    resource = "kplusk://official-library"

    def open(self) -> None:
        return None

    def close(self) -> None:
        return None

    def write(self, payload: bytes | str) -> None:
        raise Unsupported("K+K uses the official library, not a byte transport")

    def read(self) -> bytes:
        raise Unsupported("K+K uses the official library, not a byte transport")

    def query(self, payload: bytes | str) -> bytes:
        raise Unsupported("K+K uses the official library, not a byte transport")


def _connection(resource: str) -> str:
    value = resource.strip()
    if value.startswith(("kplusk://", "tcp://")):
        parsed = urlparse(value)
        if not parsed.hostname or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise DeviceError(
                "K+K resource must be an IP address or kplusk://host[:port]", "invalid_parameter"
            )
        return parsed.hostname + (f":{parsed.port}" if parsed.port else "")
    if any(char in value for char in "/\\") or not value:
        raise DeviceError("K+K resource must be an IP address or kplusk://host[:port]", "invalid_parameter")
    return value


def _runtime_candidates() -> tuple[Path, ...]:
    from lab_control.deployment.catalog import cache_directories

    here = Path(__file__).resolve()
    root = here.parents[5]
    candidates = [
        *(cache / "tools/kplusk/runtime" for cache in cache_directories()),
        root / "deployment/packages/tools/kplusk/runtime",
        root / "dist/TF-LabControl/maintenance/packages/tools/kplusk/runtime",
        Path(os.environ.get("PROGRAMDATA", "")) / "TF-LabControl/maintenance/packages/tools/kplusk/runtime",
    ]
    if getattr(sys, "frozen", False):
        candidates.insert(0, Path(sys.executable).parent / "maintenance/packages/tools/kplusk/runtime")
    return tuple(
        path
        for path in candidates
        if all((path / member).is_file() for member in ("kklib.py", "KK_Library_64.dll", "KK_FX80E.dll"))
    )


@contextlib.contextmanager
def _official_module():
    """Import the shipped wrapper and make its adjacent DLLs discoverable."""
    candidates = _runtime_candidates()
    if not candidates:
        raise DeviceError(
            "K+K runtime is missing; prepare K+K Library and Python wrapper in Environment & Repair",
            "driver_missing",
        )
    runtime = candidates[0]
    added = None
    if hasattr(os, "add_dll_directory"):
        try:
            added = os.add_dll_directory(str(runtime))
        except OSError as exc:
            raise DeviceError(f"Cannot load K+K runtime directory: {exc}", "driver_missing") from exc
    sys.path.insert(0, str(runtime))
    try:
        module = importlib.import_module("kklib")
        yield module
    except (ImportError, OSError) as exc:
        raise DeviceError(f"Cannot load official K+K Python wrapper: {exc}", "driver_missing") from exc
    finally:
        with contextlib.suppress(ValueError):
            sys.path.remove(str(runtime))
        if added is not None:
            added.close()


def _result_error(result: Any, action: str) -> None:
    code = getattr(getattr(result, "result_code", None), "name", str(getattr(result, "result_code", "")))
    if code not in {"KK_NO_ERR", "0"}:
        detail = getattr(result, "data", None) or code
        raise DeviceError(f"K+K {action} failed: {detail}", "device_error")


class KpluskFxeAdapter(ScpiAdapter):
    """Read-only K+K FXE compatibility adapter."""

    supports_live_acquisition = False
    supports_stream_acquisition = False

    def __init__(
        self, transport_or_config: Any, profile_or_library: Any, config: ConnectionConfig | None = None
    ) -> None:
        # The shared fixture harness constructs every registered adapter with
        # (transport, profile, config), while the production factory uses
        # (config, NativeLib).  Supporting both keeps registration/contract
        # tests hardware-free without weakening the explicit factory path.
        self._last_reports: list[str] = []
        self._previous_phase: Any | None = None
        if config is not None:
            super().__init__(transport_or_config, profile_or_library, config)
            self.library: Any | None = None
            self.source_id = -1
            self._mock = True
            self._closed = False
            return
        config = transport_or_config
        library = profile_or_library
        super().__init__(_NoopTransport(), profile(), config)
        self.library = library
        self.source_id = int(library.get_source_id())
        result = library.open_connection(self.source_id, _connection(config.resource), False)
        _result_error(result, "open connection")
        self._mock = False
        self._closed = False

    def _native(self) -> Any:
        if self.library is None:
            raise Unsupported("K+K mock adapter has no native library")
        return self.library

    def capabilities(self) -> CapabilitySet:
        return capability_schema(self.config)

    def identify(self) -> DeviceIdentity:
        if self._mock:
            return super().identify()
        library = self._native()
        firmware = int(library.get_firmware_version(self.source_id))
        user_id = int(library.get_user_id(self.source_id))
        identity = DeviceIdentity(
            "K+K",
            "FXE",
            serial="",
            firmware=str(firmware) if firmware >= 0 else "",
            confidence="protocol_only",
            raw=f"firmware={firmware}; user_id={user_id}",
        )
        self.identity = identity
        return identity

    def _read_reports(self, limit: int = 8) -> tuple[str, ...]:
        if self._mock:
            raise Unsupported("K+K mock adapter has no report backend")
        library = self._native()
        reports: list[str] = []
        if not hasattr(library, "get_kk_report"):
            for _ in range(limit):
                result = library.get_report(self.source_id)
                _result_error(result, "read report")
                data = getattr(result, "data", None)
                if data is None or data == "":
                    break
                reports.append(str(data))
            self._last_reports = reports
            return tuple(reports)
        for _ in range(limit):
            report = library.get_kk_report(self.source_id)
            report_type = int(getattr(report.get_report_type(), "value", report.get_report_type()))
            if report_type == 0:
                break
            if report_type == 1:
                text = getattr(report, "get_error_text", lambda: "K+K report error")() or "K+K report error"
                raise DeviceError(str(text), "device_error")
            reports.append(str(report.to_log_str()))
        self._last_reports = reports
        return tuple(reports)

    def read_status(self) -> DeviceStatus:
        if self._mock:
            return DeviceStatus({})
        if self.identity is None:
            self.identify()
        reports = self._read_reports()
        library = self._native()
        return DeviceStatus(
            {
                "connection": _connection(self.config.resource),
                "source_id": self.source_id,
                "firmware_version": int(library.get_firmware_version(self.source_id)),
                "user_id": int(library.get_user_id(self.source_id)),
                "reports": reports,
                "report_interpretation": "raw_protocol_only",
            }
        )

    def read_settings(self, operations: tuple[str, ...], channel: int = 1) -> tuple[ParameterReading, ...]:
        if self._mock:
            return super().read_settings(operations, channel)
        self._context(channel, {})
        allowed = {"firmware_version", "user_id", "report"}
        unknown = set(operations) - allowed
        if unknown:
            raise Unsupported(f"K+K settings are not available: {sorted(unknown)}")
        library = self._native()
        values: dict[str, Any] = {
            "firmware_version": int(library.get_firmware_version(self.source_id)),
            "user_id": int(library.get_user_id(self.source_id)),
            "report": self._read_reports(),
        }
        return tuple(ParameterReading(name, 1, values[name], "instrument") for name in operations)

    def apply(self, command: DeviceCommand) -> ApplyResult:
        if self._mock:
            capability = self.capabilities().get(command.operation)
            parameters = capability.validate(command.parameters)
            # The shared fixture harness uses apply as a schema-only check for
            # read-only model packages.  No vendor call is made in this path.
            return ApplyResult("success", command.operation, parameters, completed_steps=0)
        self.capabilities().get(command.operation)
        raise Unsupported("K+K FXE compatibility layer is read-only until settings protocol is verified")

    def acquire(self, request: AcquisitionRequest) -> MeasurementBatch:
        if self._mock:
            return super().acquire(request)
        request.validate()
        if request.kind != "scalar" or request.quantity != "frequency":
            raise Unsupported("K+K FXE acquisition supports scalar frequency only")
        channels = validate_channels(request.channels)
        if self.identity is None:
            self.identify()
        library = self._native()
        latest: Any | None = None
        latest_log = ""
        source_report_type = "RT_DOUBLE"
        derived_from_phase = False
        # The official library queues timestamps, messages and heartbeat
        # reports alongside measurement reports.  Drain a bounded number of
        # complete reports without sending a command or replaying a read.
        for _ in range(30):
            report = library.get_kk_report(self.source_id)
            decoded = parse_frequency_report(report)
            if decoded is not None:
                latest = decoded
                latest_log = getattr(report, "to_log_str", lambda: "")() or ""
                break
            phase = parse_phase_report(report)
            if phase is not None:
                previous = self._previous_phase
                self._previous_phase = phase
                if previous is None or len(previous.values) != len(phase.values):
                    continue
                elapsed_ms = phase.device_ms - previous.device_ms
                if elapsed_ms <= 0:
                    elapsed_ms = phase.interval_ms
                deltas = tuple(current - old for old, current in zip(previous.values, phase.values, strict=True))
                if elapsed_ms <= 0 or any(delta < 0 for delta in deltas):
                    continue
                latest = FrequencyReport(
                    phase.header,
                    phase.device_ms,
                    tuple(delta * 1000.0 / elapsed_ms for delta in deltas),
                )
                latest_log = getattr(report, "to_log_str", lambda: "")() or ""
                source_report_type = "RT_PHASE"
                derived_from_phase = True
                break
            if int(getattr(report.get_report_type(), "value", report.get_report_type())) == 0:
                # Multi_GetReportStruct is non-blocking in the shipped
                # wrapper.  Poll at a bounded cadence so the FXE2's periodic
                # measurement report is not missed between heartbeats.
                time.sleep(0.2)
                continue
        if latest is None:
            raise DeviceError("K+K did not return a frequency report", "timeout")
        selected = tuple(
            Measurement(
                "frequency",
                "Hz",
                (latest.values[channel - 1],),
                channel=channel,
                metadata={
                    "report_header": f"0x{latest.header:04X}",
                    "device_ms": latest.device_ms,
                    "report_interval_ms": latest.interval_ms,
                    "channel_count": len(latest.values),
                },
            )
            for channel in channels
            if channel <= len(latest.values)
        )
        if not selected:
            raise DeviceError("K+K frequency report has no selected channel", "sensor_missing")
        return MeasurementBatch(
            self.identity,
            selected,
            {
                "report_header": f"0x{latest.header:04X}",
                "device_ms": latest.device_ms,
                "report_interval_ms": latest.interval_ms,
                "channel_count": len(latest.values),
                "read_only": True,
                "report_log": latest_log,
                "source_report_type": source_report_type,
                "derived_from_phase": derived_from_phase,
            },
            validation="full",
            performance={
                "report_type": "phase_derived" if derived_from_phase else "double",
                "protocol": "Multi_GetReportStruct",
            },
        )

    def close(self) -> None:
        if self._mock:
            self.transport.close()
            return
        if not self._closed:
            library = self._native()
            with contextlib.suppress(Exception):
                library.close_connection(self.source_id)
            self._closed = True


def create(config: ConnectionConfig, *, library: Any | None = None) -> KpluskFxeAdapter:
    if config.transport != "kplusk":
        raise Unsupported("K+K FXE requires transport=kplusk")
    if library is not None:
        return KpluskFxeAdapter(config, library)
    with _official_module() as module:
        try:
            return KpluskFxeAdapter(config, module.NativeLib())
        except DeviceError:
            raise
        except Exception as exc:
            raise DeviceError(f"K+K library initialization failed: {exc}", "driver_missing") from exc
