from __future__ import annotations

import re
import time
from urllib.parse import urlparse

import numpy as np

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import (
    AcquisitionRequest,
    ApplyResult,
    Capability,
    CapabilitySet,
    DeviceCommand,
    DeviceError,
    DeviceIdentity,
    DeviceStatus,
    Measurement,
    MeasurementBatch,
    Parameter,
    Unsupported,
)
from lab_control.instruments.base import number
from lab_control.transport.streams import TcpSocketTransport

SOURCE = "https://ww1.microchip.com/downloads/aemDocuments/documents/FTD/ProductDocuments/UserGuides/53100A-Phase-Noise-Analyzer-Users-Guide-DS50002991.pdf"


class TserveTransport(TcpSocketTransport):
    """TimeLab command port framing, including Telnet option refusal and prompt consumption."""

    def open(self) -> None:
        super().open()
        self.read()  # Greeting and prompt only. Never starts TSERVE or a measurement.

    def read(self) -> bytes:
        data = bytearray()
        deadline = time.monotonic() + self.timeout
        while len(data) < 4 * 1024 * 1024:
            if time.monotonic() > deadline:
                raise DeviceError("TSERVE prompt timeout", "timeout")
            char = self.reader.exact(1)
            if char == b"\xff":
                verb = self.reader.exact(1)[0]
                if verb in (251, 252, 253, 254):
                    option = self.reader.exact(1)
                    if verb in (251, 253):
                        self._send(bytes([255, 254 if verb == 251 else 252]) + option)
                elif verb == 255:
                    data.extend(char)
                continue
            data.extend(char)
            if char == b">" and re.search(rb"(?:^|[\r\n])=[^\r\n>]+ >$", data):
                return bytes(data[: data.rfind(b"=")]).strip()
        raise DeviceError("Excessive TSERVE response", "parse_error")


class PhaseNoiseAdapter:
    adapter_id = "microchip.53100a"

    def __init__(self, config: ConnectionConfig, transport: TserveTransport | None = None) -> None:
        if not config.identity_confirmed:
            raise DeviceError(
                "Confirm the 53100A identity and exclusive TSERVE server ownership in configuration",
                "identity_required",
            )
        self.config = config
        endpoint = urlparse(config.resource)
        self.transport = transport or TserveTransport(
            endpoint.hostname or "", endpoint.port or 1299, config.timeout, b"\r\n"
        )
        if transport is None:
            try:
                self.transport.open()
            except Exception:
                self.transport.close()
                raise
        self.identity: DeviceIdentity | None = None

    def identify(self) -> DeviceIdentity:
        version = self.transport.query("show version").decode("ascii", errors="replace")
        if "53100" not in version and "PhaseStation" not in version:
            raise DeviceError("TSERVE did not identify a 53100A/PhaseStation", "identity_conflict")
        self.identity = DeviceIdentity(
            "Microchip",
            "53100A",
            self.config.expected_serial,
            version,
            confidence="operator_confirmed",
            raw=version,
        )
        return self.identity

    def capabilities(self) -> CapabilitySet:
        return self.capability_schema(self.config)

    @staticmethod
    def capability_schema(config: ConnectionConfig) -> CapabilitySet:
        return CapabilitySet(
            "phase_noise_analyzer",
            (1,),
            (
                Capability(
                    "start", writable=True, requires_confirmation=True, source=SOURCE, section="8.3 start"
                ),
                Capability(
                    "stop", writable=True, requires_confirmation=True, source=SOURCE, section="8.3 stop"
                ),
                Capability(
                    "input_frequency",
                    {"value": Parameter(unit="Hz", minimum=1, maximum=200e6)},
                    writable=True,
                    source=SOURCE,
                    section="8.3 set inputfreq",
                ),
                Capability(
                    "reference_frequency",
                    {"value": Parameter(unit="Hz", minimum=1, maximum=200e6)},
                    writable=True,
                    source=SOURCE,
                    section="8.3 set referencefreq",
                ),
                Capability("phase_noise", readable=True, source=SOURCE, section="8.3 show spectrum"),
                Capability("am_noise", readable=True, source=SOURCE, section="8.3 show amspectrum"),
                Capability("stability", readable=True, source=SOURCE, section="8.3 show adev"),
                Capability(
                    "data_stream",
                    readable=True,
                    source=SOURCE,
                    section="8.1 /data_port /format /timestamp",
                    available=bool(config.data_resource and config.stream_format),
                    reason="Requires explicit TSERVE data_resource, stream_format P/F, stream_interval and server /timestamp:0",
                ),
            ),
        )

    def read_status(self) -> DeviceStatus:
        return DeviceStatus(
            {
                name: self.transport.query("show " + name).decode("ascii", errors="replace")
                for name in ("state", "inputs")
            }
        )

    def read_settings(self, operations: tuple[str, ...], channel: int = 1):
        from lab_control.instruments.settings import cached_reading

        if channel != 1:
            raise Unsupported("53100A uses logical channel 1")
        for name in operations:
            self.capabilities().get(name)
        return tuple(cached_reading(name, channel, getattr(self, "settings", {})) for name in operations)

    def apply(self, command: DeviceCommand) -> ApplyResult:
        cap = self.capabilities().get(command.operation)
        values = cap.validate(command.parameters)
        if not cap.writable:
            raise Unsupported("Read-only phase noise capability")
        if command.channel != 1:
            raise Unsupported("53100A adapter uses logical channel 1")
        if cap.requires_confirmation and not command.confirmed:
            raise DeviceError("Measurement start/stop needs confirmation", "confirmation_required")
        payload = (
            command.operation
            if command.operation in {"start", "stop"}
            else (
                "set "
                + {"input_frequency": "inputfreq", "reference_frequency": "referencefreq"}[command.operation]
                + f" {values['value'] / 1e6:.12g}"
            )
        )
        try:
            result = self.transport.query(payload).decode("ascii", errors="replace")
            if any(token in result.lower() for token in ("error", "invalid", "unknown")):
                return ApplyResult("rejected", command.operation, values, result, 1)
            if not hasattr(self, "settings"):
                self.settings = {}
            self.settings[command.operation] = values
            return ApplyResult("success", command.operation, values, result, 1)
        except DeviceError as exc:
            return ApplyResult("uncertain", command.operation, values, error=str(exc))

    def acquire(self, request: AcquisitionRequest) -> MeasurementBatch:
        request.validate()
        if request.kind == "streaming":
            return self._stream(request)
        operation = request.quantity or "phase_noise"
        query = {"phase_noise": "spectrum", "am_noise": "amspectrum", "stability": "adev"}.get(operation)
        if not query or request.channels != (1,):
            raise Unsupported("Choose phase_noise, am_noise or stability on channel 1")
        if request.initiate:
            raise Unsupported("Start a TSERVE measurement explicitly before reading its results")
        raw = self.transport.query("show " + query).decode("ascii")
        sections: dict[str, list[tuple[float, float]]] = {operation: []}
        active = operation
        for line in raw.splitlines():
            if operation == "stability":
                match = re.search(r"tau:\s*(\S+)\s+adev:\s*(\S+)", line, re.IGNORECASE)
                if match:
                    sections[operation].append((number(match[1]), number(match[2])))
                continue
            if "Noise Floor" in line:
                active = "noise_floor"
                sections[active] = []
            values = line.split()
            if len(values) == 2:
                try:
                    sections[active].append((float(values[0]), float(values[1])))
                except ValueError:
                    pass
        if not sections[operation]:
            raise DeviceError("No numeric TSERVE result; measurement may not be running", "parse_error")
        measurements = []
        for quantity, pairs in sections.items():
            if pairs:
                data = np.asarray(pairs)
                measurements.append(
                    Measurement(
                        quantity,
                        "ratio" if operation == "stability" else "dBc/Hz",
                        data[:, 1],
                        axis=data[:, 0],
                        axis_unit="s" if operation == "stability" else "Hz",
                    )
                )
        if self.identity is None:
            raise DeviceError("Identify first", "identity_required")
        return MeasurementBatch(
            self.identity, tuple(measurements), {"query": query, "server": self.config.resource}
        )

    def _stream(self, request: AcquisitionRequest) -> MeasurementBatch:
        self.capabilities().get("data_stream")
        if request.initiate or request.channels != (1,) or self.identity is None:
            raise Unsupported("Identify first; read existing TSERVE stream on channel 1 without initiate")
        endpoint = urlparse(self.config.data_resource)
        if endpoint.scheme != "tcp" or not endpoint.hostname or not endpoint.port:
            raise DeviceError("Configure data_resource tcp://host:1298", "invalid_parameter")
        data = TcpSocketTransport(endpoint.hostname, endpoint.port, self.config.timeout, b"\r\n")
        values = []
        deadline = time.monotonic() + self.config.timeout
        try:
            data.open()
            for _ in range(request.points):
                if time.monotonic() > deadline:
                    raise DeviceError("TSERVE stream batch timed out", "timeout")
                # No commands are ever sent on the data port. Timestamped or warning lines fail explicitly.
                values.append(number(data.read()))
        finally:
            data.close()
        phase = self.config.stream_format == "P"
        axis = np.arange(len(values)) * self.config.stream_interval
        return MeasurementBatch(
            self.identity,
            (
                Measurement(
                    "phase_difference" if phase else "frequency",
                    "s" if phase else "Hz",
                    values,
                    axis=axis,
                    axis_unit="s",
                    metadata={
                        "axis_origin": "configured cadence; not a hardware timestamp",
                        "stream_format": self.config.stream_format,
                    },
                ),
            ),
            {"data_resource": self.config.data_resource, "stream_interval": self.config.stream_interval},
        )

    def close(self) -> None:
        self.transport.close()  # Closing a client never stops the server or physical measurement.
