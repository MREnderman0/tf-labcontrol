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
from lab_control.transport.base import block_payload, numeric_values

if TYPE_CHECKING:
    from lab_control.instruments.base import ScpiAdapter


def scaled_rigol(preamble: bytes, data: bytes, channel: int) -> Measurement:
    header = numeric_values(preamble)
    if len(header) != 10 or header[0] != 0 or header[2] < 1:
        raise DeviceError("Expected 10-value BYTE waveform preamble", "parse_error")
    values = np.frombuffer(block_payload(data), dtype=np.uint8).astype(float)
    if not values.size or values.size > int(header[2]):
        raise DeviceError("Waveform length disagrees with preamble", "parse_error")
    y = (values - header[9] - header[8]) * header[7]
    x = (np.arange(values.size) - header[6]) * header[4] + header[5]
    return Measurement("voltage", "V", y, channel, x, "s", metadata={"preamble": header.tolist()})


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
    for channel in request.channels:
        transport.write(f"WAV:SOUR CHAN{channel}")
        transport.write("WAV:MODE NORM")
        transport.write("WAV:FORM BYTE")
        preamble = transport.query("WAV:PRE?")
        result.append(scaled_rigol(preamble, transport.query("WAV:DATA?"), channel))
    adapter.configuration["transfer"] = "BYTE; NORM screen waveform"
    return tuple(result)
