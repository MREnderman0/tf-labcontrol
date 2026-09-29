"""Finite, gap-checked phase acquisition; all instrument commands stay here."""

import re
import socket
import time
from collections.abc import Iterator
from dataclasses import replace
from threading import Event
from typing import TYPE_CHECKING

import numpy as np

from lab_control.domain.instruments import (
    AcquisitionRequest,
    DeviceCommand,
    DeviceError,
    Measurement,
    MeasurementBatch,
    utc_now,
)

from .profile import FREQUENCIES
from .stability import deviations

if TYPE_CHECKING:
    from .adapter import Microchip53100AAdapter


def validate_run(adapter: "Microchip53100AAdapter", request: AcquisitionRequest) -> None:
    request.validate()
    if (
        request.kind != "timed_phase"
        or not request.initiate
        or not request.confirmed
        or request.channels != (1,)
    ):
        raise DeviceError(
            "Finite phase acquisition requires explicit start on channel 1", "confirmation_required"
        )
    adapter.capabilities().get("timed_phase")
    if adapter.identity is None or adapter.server is None:
        raise DeviceError("Connect the managed USB instrument first", "identity_required")
    if request.sample_rate not in (1, 10, 100, 1000) or request.duration is None:
        raise DeviceError("Choose sample rate 1 / 10 / 100 / 1000 and duration", "invalid_parameter")
    count = request.sample_rate * request.duration
    if not 10 <= request.duration <= 86400 or count != int(count) or count + 1 > 4_000_000:
        raise DeviceError(
            "Duration must be 10–86400 s with at most 4,000,000 phase samples", "invalid_parameter"
        )


class PhaseStream:
    """One persistent socket, bounded framing; socket waits are cancellable."""

    def __init__(self, port: int, rate: float, timeout: float):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout)
        self.sock.settimeout(0.2)
        self.buffer = bytearray()
        self.previous: float | None = None
        self.period = 1 / rate

    def read(self, stop: Event, timeout: float) -> tuple[float, float] | None:
        deadline = time.monotonic() + timeout
        while not stop.is_set():
            if b"\n" in self.buffer:
                raw, _, remaining = self.buffer.partition(b"\n")
                self.buffer = bytearray(remaining)
                try:
                    fields = raw.decode("ascii", errors="strict").split()
                    if len(fields) != 2:
                        raise ValueError("Expected timestamp and phase; possible instrument warning")
                    stamp, phase = map(float, fields)
                except (ValueError, UnicodeError) as exc:
                    raise DeviceError(
                        "Invalid phase data / TSERVE notification: " + repr(raw[:256]), "parse_error"
                    ) from exc
                if not np.isfinite((stamp, phase)).all() or stamp < 0:
                    raise DeviceError("Non-finite phase data", "parse_error")
                if self.previous is not None and abs(stamp - self.previous - self.period) > 1.1e-6:
                    raise DeviceError("Phase timestamp gap, duplicate or sample-rate mismatch", "data_gap")
                self.previous = stamp
                return stamp, phase
            if len(self.buffer) > 65536:
                raise DeviceError("Excessive phase stream row", "parse_error")
            if time.monotonic() >= deadline:
                raise DeviceError("No phase samples within the acquisition deadline", "timeout")
            try:
                data = self.sock.recv(65536)
            except TimeoutError:
                continue
            except OSError as exc:
                raise DeviceError(str(exc), "disconnected") from exc
            if not data:
                raise DeviceError("TSERVE data channel closed", "disconnected")
            self.buffer.extend(data)
        return None

    def close(self) -> None:
        self.sock.close()


def spectrum(raw: str) -> tuple[Measurement, ...]:
    """Exact documented headers, no silent dropping of malformed numeric rows."""
    sections: dict[str, list[tuple[float, float]]] = {"phase_noise": []}
    active = "phase_noise"
    headers = {
        "Time Constant: Infinite",
        "Frequency (Hz) Power Spectral Density (dBc/Hz)",
        "Frequency (Hz) PSD",
    }
    for line in raw.splitlines():
        line = " ".join(line.split())
        if not line or line in headers:
            continue
        if line == "Noise Floor":
            active = "noise_floor"
            if active in sections:
                raise DeviceError("Repeated noise floor section", "parse_error")
            sections[active] = []
            continue
        try:
            fields = line.split()
            if len(fields) != 2:
                raise ValueError(line)
            x, y = map(float, fields)
        except ValueError as exc:
            raise DeviceError("Invalid TSERVE spectrum row: " + line, "parse_error") from exc
        if not np.isfinite((x, y)).all() or x <= 0 or (sections[active] and x <= sections[active][-1][0]):
            raise DeviceError("Invalid TSERVE spectrum axis/value", "parse_error")
        sections[active].append((x, y))
    if not sections["phase_noise"] and any(sections.values()):
        raise DeviceError("Noise floor without a valid phase spectrum", "parse_error")
    return tuple(
        Measurement(name, "dBc/Hz", np.asarray(rows)[:, 1], axis=np.asarray(rows)[:, 0], axis_unit="Hz")
        for name, rows in sections.items()
        if rows
    )


def acquire_run(
    adapter: "Microchip53100AAdapter", request: AcquisitionRequest, stop: Event
) -> Iterator[MeasurementBatch]:
    from .adapter import is_ready, serial_response

    validate_run(adapter, request)
    assert adapter.identity is not None and request.sample_rate is not None and request.duration is not None
    if not is_ready(adapter._query("show state")):
        raise DeviceError("Stop the existing measurement before starting another", "task_busy")
    settings = {r.operation: r.value for r in adapter.read_settings(tuple(FREQUENCIES))}
    original_rate = adapter.read_sample_rate()
    rate = request.sample_rate
    total = int(rate * request.duration) + 1
    values = np.empty(total, dtype=np.float64)
    acquired = 0
    first_stamp = 0.0
    latest_spectrum: tuple[Measurement, ...] = ()
    stream = None
    started = False
    verified = False
    base = {
        "sample_rate": rate,
        "requested_duration": request.duration,
        "settings": settings,
        "original_sample_rate": original_rate,
        "phase_format": "P",
        "timestamp_format": "s",
        "decimation": 2,
        "phase_channels": "J3-J1",
        "spectrum_smoothing": False,
        "source": "TSERVE continuous phase stream",
        "hardware_validation": "pending_signal_test",
    }

    def config(**extra):
        return {**base, "samples": acquired, "elapsed": max(0, acquired - 1) / rate, **extra}

    def check_state():
        if not re.search(r"\bCollecting\b", adapter._query("show state"), re.I):
            raise DeviceError("Measurement left Collecting state", "acquisition_unavailable")
        if adapter.read_sample_rate() != rate or settings != {
            r.operation: r.value for r in adapter.read_settings(tuple(FREQUENCIES))
        }:
            raise DeviceError("Phase acquisition configuration changed; run stopped", "configuration_changed")

    try:
        if stop.is_set():
            return
        if original_rate != rate:
            applied = adapter.apply(DeviceCommand("sample_rate", {"value": int(rate)}))
            if applied.outcome != "success":
                raise DeviceError(
                    "Sample-rate write/readback failed; no replay: " + applied.error,
                    "configuration_uncertain",
                )
        stream = PhaseStream(adapter.server.data_port, rate, adapter.config.timeout)
        if stop.is_set():
            return
        started = True  # Includes an uncertain start: cleanup is attempted once, never replayed.
        adapter._query("start")
        while acquired < total and not stop.is_set():
            rows = []
            for _ in range(min(int(rate), total - acquired)):
                sample = stream.read(stop, 30 if acquired == 0 else max(3, adapter.config.timeout))
                if sample is None:
                    break
                stamp, phase = sample
                if acquired == 0:
                    first_stamp = stamp
                rows.append((stamp, phase))
                values[acquired] = phase
                acquired += 1
            if not rows:
                break
            check_state()
            if not verified:
                actual = serial_response(adapter._query("show serial"))
                if actual != adapter.identity.serial:
                    raise DeviceError(
                        "Active acquisition serial differs from selected USB instrument", "identity_conflict"
                    )
                verified = True
            new_spectrum: tuple[Measurement, ...] = ()
            if acquired == total or (acquired // int(rate)) % 5 == 0:
                parsed = spectrum(adapter._query("show spectrum"))
                check_state()
                queried_at = utc_now()
                latest_spectrum = tuple(
                    replace(m, metadata={"queried_at": queried_at, "phase_samples_at_query": acquired})
                    for m in parsed
                )
                new_spectrum = latest_spectrum
            data = np.asarray(rows)
            yield MeasurementBatch(
                adapter.identity,
                (
                    Measurement(
                        "phase_difference",
                        "s",
                        data[:, 1],
                        axis=data[:, 0],
                        axis_unit="s",
                        metadata={"timestamp_source": "TSERVE sample counter", "continuous": True},
                    ),
                )
                + new_spectrum,
                config(complete=False, final=False, first_timestamp=first_stamp),
                validation="full",
            )
    finally:
        if stream is not None:
            stream.close()
        if started:
            try:
                adapter._query("stop")
                if not is_ready(adapter._query("show state")):
                    raise DeviceError("TSERVE did not return to Ready", "cleanup_failed")
            except Exception as exc:
                raise DeviceError(
                    "Measurement stop unconfirmed; final instrument state unknown: " + str(exc),
                    "cleanup_failed",
                ) from exc
    if acquired:
        stability = deviations(values[:acquired], rate)
        yield MeasurementBatch(
            adapter.identity,
            latest_spectrum + stability,
            config(
                complete=acquired == total,
                final=True,
                stopped=True,
                stop_state="Ready",
                phase_noise_available=any(m.quantity == "phase_noise" for m in latest_spectrum),
                stability_available=bool(stability),
                first_timestamp=first_stamp,
            ),
            validation="full",
        )
