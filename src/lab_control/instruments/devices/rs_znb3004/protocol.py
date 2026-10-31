from __future__ import annotations

from typing import TYPE_CHECKING

from lab_control.domain.instruments import (
    AcquisitionRequest,
    DeviceCommand,
    DeviceError,
    Measurement,
    Unsupported,
)
from lab_control.transport.base import numeric_values

if TYPE_CHECKING:
    from lab_control.instruments.base import ScpiAdapter


def complex_trace(raw: bytes, axis: bytes, channel: int = 1) -> Measurement:
    values, frequencies = numeric_values(raw), numeric_values(axis)
    if values.size != frequencies.size * 2 or frequencies.size == 0:
        raise DeviceError("S parameter real/imaginary count differs from frequency axis", "parse_error")
    return Measurement(
        "selected_s_parameter", "ratio", values[::2] + 1j * values[1::2], channel, frequencies, "Hz"
    )


def acquire(adapter: ScpiAdapter, request: AcquisitionRequest) -> tuple[Measurement, ...]:
    transport = adapter.transport
    if request.kind != "s_parameters":
        raise Unsupported("Use acquisition kind=s_parameters for this model")
    if request.initiate:
        continuous = adapter.apply(DeviceCommand("continuous", {"value": False}, confirmed=True))
        if continuous.outcome != "success":
            raise DeviceError(continuous.error, "configuration_uncertain")
        applied = adapter.apply(DeviceCommand("single", confirmed=True))
        if applied.outcome != "success":
            raise DeviceError(applied.error, "configuration_uncertain")
        transport.query("*OPC?")
    result: list[Measurement] = []
    transport.write("FORM ASC")
    result.append(complex_trace(transport.query("CALC1:DATA? SDAT"), transport.query("CALC1:DATA:STIM?")))
    adapter.configuration["correction_status"] = adapter.read("correction_status")
    adapter.configuration["trace_catalog"] = adapter.read("trace_catalog")
    adapter.configuration["selected_trace"] = adapter.read("selected_trace")
    return tuple(result)
