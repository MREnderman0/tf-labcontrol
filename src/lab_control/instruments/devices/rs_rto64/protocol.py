from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

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


def acquire(adapter: ScpiAdapter, request: AcquisitionRequest) -> tuple[Measurement, ...]:
    transport = adapter.transport
    if request.kind != "waveform":
        raise Unsupported("Use acquisition kind=waveform for this model")
    if request.initiate:
        applied = adapter.apply(DeviceCommand("single", confirmed=True))
        if applied.outcome != "success":
            raise DeviceError(applied.error, "configuration_uncertain")
        transport.query("*OPC?")
    result: list[Measurement] = []
    transport.write("FORM ASC")
    transport.write("EXPort:WAVeform:MULTichannel OFF")
    transport.write("EXPort:WAVeform:INCXvalues OFF")
    for channel in request.channels:
        header = numeric_values(transport.query(f"CHAN{channel}:DATA:HEAD?"))
        if header.size != 4 or header[3] != 1 or (not 1 <= header[2] <= 4000000):
            raise DeviceError("Unsupported/invalid waveform header or peak/envelope data", "parse_error")
        values = numeric_values(transport.query(f"CHAN{channel}:DATA?"))
        if values.size != int(header[2]):
            raise DeviceError("Waveform/header point count mismatch", "parse_error")
        axis = np.linspace(header[0], header[1], values.size)
        result.append(
            Measurement("voltage", "V", values, channel, axis, "s", metadata={"header": header.tolist()})
        )
    adapter.configuration["transfer"] = "ASCII voltage samples"
    return tuple(result)
