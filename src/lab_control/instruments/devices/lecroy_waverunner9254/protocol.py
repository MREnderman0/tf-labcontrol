from __future__ import annotations

import struct
from typing import TYPE_CHECKING

import numpy as np

from lab_control.domain.instruments import (
    AcquisitionRequest,
    DeviceCommand,
    DeviceError,
    Measurement,
    Unsupported,
)
from lab_control.transport.base import block_payload

if TYPE_CHECKING:
    from lab_control.instruments.base import ScpiAdapter


def lecroy_waveform(raw: bytes, channel: int) -> Measurement:
    # C1:WF ALL responses may include an ASCII command prefix before the IEEE block.
    marker = raw.find(b"#")
    if marker >= 0:
        raw = block_payload(raw[marker:], allow_indefinite=True)
    start = raw.find(b"WAVEDESC")
    if start < 0 or len(raw) - start < 346:
        raise DeviceError("Missing/truncated LeCroy WAVEDESC", "parse_error")
    raw = raw[start:]
    endian = "<" if raw[34:36] == b"\x01\x00" else ">" if raw[34:36] == b"\x00\x00" else ""
    if not endian:
        raise DeviceError("Unknown LeCroy byte order", "parse_error")

    def unpack(fmt: str, offset: int):
        return struct.unpack_from(endian + fmt, raw, offset)[0]

    width = unpack("h", 32)
    lengths = [unpack("i", offset) for offset in (36, 40, 44, 48, 52, 56)]
    size, count = unpack("i", 60), unpack("i", 116)
    segments = unpack("i", 144)
    if segments > 1:
        raise Unsupported("Segmented LeCroy acquisition requires segment timestamps; select a single segment")
    offset = sum(lengths)
    if min(lengths) < 0 or width not in (0, 1) or size != count * (width + 1) or offset + size > len(raw):
        raise DeviceError("Inconsistent LeCroy descriptor lengths", "parse_error")
    values = np.frombuffer(raw[offset : offset + size], dtype=endian + ("i2" if width else "i1")).astype(
        float
    )
    first, last = unpack("i", 124), unpack("i", 128)
    if not 0 <= first <= last < count:
        raise DeviceError("Invalid LeCroy valid point range", "parse_error")
    y = values[first : last + 1] * unpack("f", 156) - unpack("f", 160)
    x = np.arange(first, last + 1) * unpack("f", 176) + unpack("d", 180)
    unit = raw[196:244].split(b"\0")[0].decode("ascii")
    return Measurement("waveform", unit, y, channel, x, "s", metadata={"descriptor_length": lengths[0]})


def acquire(adapter: ScpiAdapter, request: AcquisitionRequest) -> tuple[Measurement, ...]:
    transport = adapter.transport
    if request.kind != "waveform":
        raise Unsupported("Use acquisition kind=waveform for this model")
    if request.initiate:
        applied = adapter.apply(DeviceCommand("single", confirmed=True))
        if applied.outcome != "success":
            raise DeviceError(applied.error, "acquisition_failed")
        transport.query("*OPC?")
    result: list[Measurement] = []
    transport.write("COMM_HEADER OFF")
    transport.write("COMM_FORMAT DEF9,WORD,BIN")
    for channel in request.channels:
        result.append(lecroy_waveform(transport.query(f"C{channel}:WF? ALL"), channel))
    adapter.configuration["transfer"] = "DEF9 WORD BIN; descriptor scaling"
    return tuple(result)
