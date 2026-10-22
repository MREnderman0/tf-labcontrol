"""53100A protocol. TSERVE configuration readback is not a physical measurement."""

import re
import time
from typing import Any, Callable
from urllib.parse import urlparse

import numpy as np

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import (
    AcquisitionRequest,
    ApplyResult,
    DeviceCommand,
    DeviceError,
    DeviceIdentity,
    DeviceStatus,
    Measurement,
    MeasurementBatch,
    ParameterReading,
    Unsupported,
    ValidationError,
)
from lab_control.instruments.base import ScpiAdapter, number
from lab_control.transport.streams import TcpSocketTransport

from .profile import FREQUENCIES, capability_schema, profile
from .transport import TserveTransport


def serial_response(raw: str) -> str:
    value = re.sub(r"^(?:serial(?:\s+number)?|s/n)\s*[#:=]?\s*", "", raw.strip(), flags=re.I)
    value = value.strip().strip('"')
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{1,127}", value) or value.upper() in {
        "UNKNOWN",
        "N/A",
        "NONE",
        "NOT_FOUND",
        "ERROR",
    }:
        raise DeviceError("TSERVE 未返回可核验的设备序列号：" + raw, "identity_required")
    return value


def frequency_response(raw: str) -> dict[str, Any]:
    value = raw.strip()
    # Query echoes / prompts are consumed at the transport boundary. Accept only the declared value.
    value = re.sub(
        r"^(?:inputfreq|referencefreq|(?:input|reference)\s+frequency)\s*[:=]\s*", "", value, flags=re.I
    )
    if value.casefold() in {"auto", "automatic"}:
        return {"automatic": True, "value": None}
    automatic = value.lower().endswith("(auto)")
    if automatic:
        value = value[:-6].strip()
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?)\s*(MHz|Hz)?", value, re.I)
    if not match:
        raise DeviceError("Invalid TSERVE frequency readback: " + raw, "parse_error")
    hz = number(match[1]) * (1 if (match[2] or "MHz").upper() == "HZ" else 1e6)
    if not 1e6 <= hz <= 200e6:
        raise DeviceError("TSERVE frequency readback out of range", "parse_error")
    return {"automatic": automatic, "value": None if automatic else hz}


def is_ready(raw: str) -> bool:
    # Current TSERVE appends a separate "Time Constant: Infinite" status line.
    lines = raw.strip().splitlines()
    return bool(lines and re.fullmatch(r"(?:State:\s*)?Ready", lines[0].strip(), re.I))


class Microchip53100AAdapter(ScpiAdapter):
    recorded_run_kinds = ("timed_phase",)
    server: Any = None
    service_inspected = False
    capability_schema = staticmethod(capability_schema)

    def capabilities(self):
        return capability_schema(self.config)

    def _query(self, command: str) -> str:
        try:
            raw = self.transport.query(command).decode("ascii", errors="strict").strip()
        except Exception:
            self.transport.close()
            raise
        # Servers may echo the command; only remove an exact first line.
        lines = raw.splitlines()
        if lines and lines[0].strip().casefold() == command.casefold():
            raw = "\n".join(lines[1:]).strip()
        if re.search(r"\b(error|invalid|unknown command|not supported)\b", raw, re.I):
            raise DeviceError(raw, "command_rejected")
        return raw

    def identify(self) -> DeviceIdentity:
        version = self._query("show version")
        if not re.search(r"\b53100A\b", version, re.I):
            raise DeviceError("TSERVE 型号与 53100A 不符", "identity_conflict")
        usb_identity = None
        try:
            serial = serial_response(self._query("show serial"))
        except DeviceError as exc:
            if "no active connection" in str(exc).lower():
                if self.server is None:
                    raise DeviceError(
                        "外部 TSERVE 空闲时不提供硬件序列号；无法独立核验 USB 身份", "identity_required"
                    ) from exc
                usb_identity = self.server.verify_identity()
                serial = usb_identity["serial"]
            else:
                raise
        if self.config.expected_serial and serial != self.config.expected_serial:
            raise DeviceError("TSERVE 设备序列号与选定目标不符", "identity_conflict")
        state = self._query("show state")
        if usb_identity is not None and not is_ready(state):
            raise DeviceError("USB 身份后备路径仅用于自有空闲服务", "identity_required")
        if re.search(r"not found|no instrument|disconnected", state, re.I):
            raise DeviceError("TSERVE 未连接设备：" + state, "disconnected")
        firmware = re.search(r"(?:firmware|FPGA)\s*(?:version)?\s*[:=]\s*([^\r\n]+)", version, re.I)
        self.capability_details = {
            "server_version": version,
            "identity_evidence": "USB GET_DESCRIPTOR" if usb_identity is not None else "show serial",
            "state": state,
            "usb_identity": usb_identity,
            "hardware_acquisition_open": usb_identity is None,
        }
        self.identity = DeviceIdentity(
            "Microchip",
            "53100A",
            serial,
            firmware[1].strip() if firmware else "",
            confidence="usb_descriptor" if usb_identity is not None else "device_reported",
            raw=version + "\nserial=" + serial + "\n" + state,
        )
        return self.identity

    def inspect_service(self) -> dict[str, str]:
        """Inspect an owned server for configuration-only maintenance, not identity."""
        version = self._query("show version")
        state = self._query("show state")
        if not re.search(r"\b53100A\b", version, re.I) or not is_ready(state):
            raise DeviceError("TSERVE service is not an idle 53100A service", "task_busy")
        self.service_inspected = True
        return {"server_version": version, "state": state, "identity": "not_verified"}

    def read_status(self) -> DeviceStatus:
        # show message clears the driver message on current releases, so never include it here.
        return DeviceStatus({"state": self._query("show state"), "inputs": self._query("show inputs")})

    def read_settings(self, operations: tuple[str, ...], channel: int = 1) -> tuple[ParameterReading, ...]:
        self._context(channel, {})
        if any(name not in (*FREQUENCIES, "sample_rate") for name in operations):
            raise Unsupported("Only input/reference frequency settings have configuration readback")
        return tuple(
            ParameterReading(
                name,
                channel,
                {"value": self.read_sample_rate()}
                if name == "sample_rate"
                else frequency_response(self._query("show " + FREQUENCIES[name])),
                "service_readback",
            )
            for name in operations
        )

    def read_sample_rate(self) -> int:
        raw = self._query("show phaserate")
        match = re.fullmatch(r"Network port phase output rate:\s*(1|10|100|1000) samples/sec", raw)
        if not match:
            raise DeviceError("Invalid TSERVE sample rate readback: " + raw, "parse_error")
        return int(match[1])

    def validate_recorded_run(self, request: AcquisitionRequest) -> None:
        from .measurement import validate_run

        validate_run(self, request)

    def run_acquisition(self, request: AcquisitionRequest, stop):
        from .measurement import acquire_run

        return acquire_run(self, request, stop)

    def apply(self, command: DeviceCommand) -> ApplyResult:
        self._context(command.channel, {})
        cap = self.capabilities().get(command.operation)
        values = cap.validate(command.parameters)
        if not cap.writable:
            raise Unsupported("Read-only phase noise capability")
        if self.identity is None and not (self.service_inspected and command.operation in FREQUENCIES):
            raise DeviceError("Identify device before writing", "identity_required")
        if cap.requires_confirmation and not command.confirmed:
            raise DeviceError("Measurement start/stop needs confirmation", "confirmation_required")
        if command.operation == "sample_rate":
            if not is_ready(self._query("show state")):
                raise DeviceError("Stop measurement before changing sample rate", "task_busy")
            requested = values
            payload = f"set phaserate {values['value']}"
        elif command.operation in FREQUENCIES:
            automatic = values.get("automatic", False)
            if not automatic and "value" not in values:
                raise ValidationError("Manual frequency requires value in Hz")
            state = self._query("show state")
            if not is_ready(state):
                raise DeviceError(
                    "Stop measurement before modifying its frequency configuration", "task_busy"
                )
            requested = {"automatic": automatic, "value": None if automatic else values["value"]}
            setting = "auto" if automatic else f"{values['value'] / 1e6:.12g}"
            payload = "set " + FREQUENCIES[command.operation] + " " + setting
        else:
            requested, payload = values, command.operation
        written = False
        try:
            written = True
            response = self._query(payload)
            if command.operation in (*FREQUENCIES, "sample_rate"):
                actual = self.read_settings((command.operation,), command.channel)[0].value
                if actual != requested:
                    return ApplyResult(
                        "uncertain",
                        command.operation,
                        requested,
                        actual,
                        error="TSERVE readback mismatch",
                        completed_steps=1,
                    )
                return ApplyResult("success", command.operation, requested, actual, 1)
            return ApplyResult("success", command.operation, requested, response, 1)
        except DeviceError as exc:
            return ApplyResult(
                "rejected" if exc.code == "command_rejected" else "uncertain",
                command.operation,
                requested,
                completed_steps=int(written),
                error=str(exc),
            )

    def acquire(self, request: AcquisitionRequest) -> MeasurementBatch:
        request.validate()
        if self.identity is None or request.channels != (1,) or request.initiate:
            raise Unsupported("Identify first; explicitly start measurement before passive channel-1 reads")
        if request.kind == "streaming":
            return self._stream(request)
        quantity = request.quantity or "phase_noise"
        query = {"phase_noise": "spectrum", "am_noise": "amspectrum", "stability": "adev"}.get(quantity)
        if query is None:
            raise Unsupported("Choose phase_noise, am_noise or stability")
        before = self._query("show state")
        if not re.search(r"\bCollecting\b", before, re.I):
            raise DeviceError("No active measurement data", "acquisition_unavailable")
        settings = {r.operation: r.value for r in self.read_settings(tuple(FREQUENCIES))}
        raw = self._query("show " + query)
        after = self._query("show state")
        if not re.search(r"\bCollecting\b", after, re.I):
            raise DeviceError("Measurement state changed during read", "configuration_changed")
        final_settings = {r.operation: r.value for r in self.read_settings(tuple(FREQUENCIES))}
        if final_settings != settings:
            raise DeviceError("Frequency configuration changed during read", "configuration_changed")
        if quantity == "phase_noise":
            from .measurement import spectrum

            spectrum_measurements = spectrum(raw)
            if not spectrum_measurements:
                raise DeviceError("No complete numeric TSERVE result", "parse_error")
            return MeasurementBatch(
                self.identity,
                spectrum_measurements,
                {"query": query, "settings": settings},
                validation="full",
            )
        sections: dict[str, list[tuple[float, float]]] = {quantity: []}
        active = quantity
        for line in raw.splitlines():
            values: tuple[str, ...]
            line = line.strip()
            if not line:
                continue
            if "Noise Floor" in line:
                active = "noise_floor"
                sections[active] = []
                continue
            if quantity == "stability":
                match = re.fullmatch(r"tau:\s*(\S+)\s+adev:\s*(\S+)", line, re.I)
                if not match:
                    raise DeviceError("Malformed TSERVE ADEV row", "parse_error")
                values = (match[1], match[2])
            else:
                values = tuple(line.split())
                if len(values) != 2:
                    raise DeviceError("Malformed TSERVE spectrum row", "parse_error")
            x, y = number(values[0]), number(values[1])
            if (
                x <= 0
                or (quantity == "stability" and y < 0)
                or (sections[active] and x <= sections[active][-1][0])
            ):
                raise DeviceError("Invalid TSERVE axis/data", "parse_error")
            sections[active].append((x, y))
        if not sections[quantity] or any(not pairs for pairs in sections.values()):
            raise DeviceError("No complete numeric TSERVE result", "parse_error")
        measurements = []
        for name, pairs in sections.items():
            data = np.asarray(pairs)
            measurements.append(
                Measurement(
                    name,
                    "ratio" if quantity == "stability" else "dBc/Hz",
                    data[:, 1],
                    axis=data[:, 0],
                    axis_unit="s" if quantity == "stability" else "Hz",
                )
            )
        return MeasurementBatch(
            self.identity, tuple(measurements), {"query": query, "settings": settings}, validation="full"
        )

    def _stream(self, request: AcquisitionRequest) -> MeasurementBatch:
        self.capabilities().get("data_stream")
        endpoint = urlparse(self.config.data_resource)
        if endpoint.scheme != "tcp" or not endpoint.hostname or not endpoint.port:
            raise ValidationError("Configure data_resource tcp://host:port")
        if not re.search(r"\bCollecting\b", self._query("show state"), re.I):
            raise DeviceError("No active measurement data", "acquisition_unavailable")
        settings = {r.operation: r.value for r in self.read_settings(tuple(FREQUENCIES))}
        data = TcpSocketTransport(endpoint.hostname, endpoint.port, self.config.timeout, b"\r\n")
        values = []
        deadline = time.monotonic() + self.config.timeout
        try:
            data.open()
            for _ in range(request.points):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DeviceError("TSERVE stream batch timed out", "timeout")
                if data.sock:
                    data.sock.settimeout(remaining)
                values.append(number(data.read()))
        finally:
            data.close()
        if not re.search(r"\bCollecting\b", self._query("show state"), re.I) or settings != {
            r.operation: r.value for r in self.read_settings(tuple(FREQUENCIES))
        }:
            raise DeviceError("Configuration changed during data stream", "configuration_changed")
        assert self.identity is not None
        phase = self.config.stream_format == "P"
        return MeasurementBatch(
            self.identity,
            (
                Measurement(
                    "phase_difference" if phase else "frequency",
                    "s" if phase else "Hz",
                    values,
                    axis=np.arange(len(values)) * self.config.stream_interval,
                    axis_unit="s",
                    metadata={"axis_origin": "configured cadence; not a hardware timestamp"},
                ),
            ),
            {"data_resource": self.config.data_resource},
            validation="full",
        )

    def close(self) -> None:
        self.transport.close()
        if self.server is not None:
            self.server.close()
            self.server = None


def create(
    config: ConnectionConfig, *, trace: Callable[[dict], None] | None = None
) -> Microchip53100AAdapter:
    if config.transport != "tserve":
        raise Unsupported("53100A requires TimeLab TSERVE, not VISA/SCPI")
    server = None
    if config.usb_instance_id:
        from .server import ManagedServer

        server = ManagedServer(config)
        endpoint_url = server.start()
    else:
        if not config.identity_confirmed:
            raise DeviceError("Confirm exclusive external TSERVE ownership", "identity_required")
        endpoint_url = config.resource
    transport = None
    try:
        endpoint = urlparse(endpoint_url)
        if (
            endpoint.scheme != "tcp"
            or not endpoint.hostname
            or endpoint.path not in ("", "/")
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValidationError("Use tcp://host:port for external TSERVE")
        transport = TserveTransport(
            endpoint.hostname, endpoint.port or 1299, config.timeout, b"\r\n", trace=trace
        )
        transport.open()
        result = Microchip53100AAdapter(transport, profile(), config)
        result.server = server
        return result
    except Exception:
        if transport:
            transport.close()
        if server:
            server.close()
        raise
