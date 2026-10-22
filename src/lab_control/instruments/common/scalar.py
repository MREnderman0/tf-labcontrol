"""Scalar acquisition for devices exposing measured_voltage/current capabilities."""

from lab_control.domain.instruments import AcquisitionRequest, Measurement, Unsupported
from lab_control.instruments.base import ScpiAdapter, number


def acquire_power(adapter: ScpiAdapter, request: AcquisitionRequest) -> tuple[Measurement, ...]:
    if request.kind != "scalar":
        raise Unsupported("Use acquisition kind=scalar for this model")
    return tuple(
        Measurement(quantity, unit, [number(str(adapter.read("measured_" + quantity, channel)))], channel)
        for channel in request.channels
        for quantity, unit in (("voltage", "V"), ("current", "A"))
    )
