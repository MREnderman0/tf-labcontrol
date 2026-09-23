from __future__ import annotations

import re
import struct
import time
from typing import TYPE_CHECKING

import numpy as np

from lab_control.domain.instruments import (
    AcquisitionRequest,
    DeviceCommand,
    DeviceError,
    Measurement,
    Quality,
    Unsupported,
)
from lab_control.instruments.base import number
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


def complex_trace(raw: bytes, axis: bytes, channel: int = 1) -> Measurement:
    values, frequencies = numeric_values(raw), numeric_values(axis)
    if values.size != frequencies.size * 2 or frequencies.size == 0:
        raise DeviceError("S parameter real/imaginary count differs from frequency axis", "parse_error")
    return Measurement(
        "selected_s_parameter", "ratio", values[::2] + 1j * values[1::2], channel, frequencies, "Hz"
    )


def acquire(adapter: ScpiAdapter, request: AcquisitionRequest) -> tuple[Measurement, ...]:
    profile, transport = adapter.profile, adapter.transport
    family = profile.acquisition
    if not family:
        raise Unsupported("This instrument does not expose a measured data acquisition route")
    kinds = {
        "power": "scalar",
        "dmm": "scalar",
        "dsa": "spectrum",
        "fph": "spectrum",
        "vna": "s_parameters",
        "rigol_scope": "waveform",
        "rtm": "waveform",
        "rto": "waveform",
        "lecroy": "waveform",
    }
    if request.kind != kinds[family]:
        raise Unsupported(f"Use acquisition kind={kinds[family]} for this model")
    if family == "dmm" and not request.confirmed:
        raise DeviceError(
            "3458A capture needs confirmation: RMEM stops memory recording; initiate reconfigures triggering",
            "confirmation_required",
        )
    if request.initiate and family not in {"power", "dmm"}:
        if family in {"dsa", "fph", "vna"}:
            continuous = adapter.apply(DeviceCommand("continuous", {"value": False}, confirmed=True))
            if continuous.outcome != "success":
                raise DeviceError(continuous.error, "acquisition_failed")
        applied = adapter.apply(DeviceCommand("single", confirmed=True))
        if applied.outcome != "success":
            raise DeviceError(applied.error, "acquisition_failed")
        transport.query("*OPC?")
    result: list[Measurement] = []
    if family == "power":
        for channel in request.channels:
            for quantity, unit in (("voltage", "V"), ("current", "A")):
                value = number(str(adapter.read("measured_" + quantity, channel)))
                result.append(Measurement(quantity, unit, [value], channel))
    elif family == "rigol_scope":
        for channel in request.channels:
            transport.write(f"WAV:SOUR CHAN{channel}")
            transport.write("WAV:MODE NORM")
            transport.write("WAV:FORM BYTE")
            preamble = transport.query("WAV:PRE?")
            result.append(scaled_rigol(preamble, transport.query("WAV:DATA?"), channel))
        adapter.configuration["transfer"] = "BYTE; NORM screen waveform"
    elif family in {"rtm", "rto"}:
        transport.write("FORM ASC")
        if family == "rto":
            transport.write("EXPort:WAVeform:MULTichannel OFF")
            transport.write("EXPort:WAVeform:INCXvalues OFF")
        for channel in request.channels:
            header = numeric_values(transport.query(f"CHAN{channel}:DATA:HEAD?"))
            if header.size != 4 or header[3] != 1 or not 1 <= header[2] <= 4_000_000:
                raise DeviceError("Unsupported/invalid waveform header or peak/envelope data", "parse_error")
            values = numeric_values(transport.query(f"CHAN{channel}:DATA?"))
            if values.size != int(header[2]):
                raise DeviceError("Waveform/header point count mismatch", "parse_error")
            if family == "rtm":
                origin = number(transport.query(f"CHAN{channel}:DATA:XOR?"))
                increment = number(transport.query(f"CHAN{channel}:DATA:XINC?"))
                axis = origin + np.arange(values.size) * increment
            else:
                axis = np.linspace(header[0], header[1], values.size)
            result.append(
                Measurement("voltage", "V", values, channel, axis, "s", metadata={"header": header.tolist()})
            )
        adapter.configuration["transfer"] = "ASCII voltage samples"
    elif family == "lecroy":
        transport.write("COMM_HEADER OFF")
        transport.write("COMM_FORMAT DEF9,WORD,BIN")
        for channel in request.channels:
            result.append(lecroy_waveform(transport.query(f"C{channel}:WF? ALL"), channel))
        adapter.configuration["transfer"] = "DEF9 WORD BIN; descriptor scaling"
    elif family in {"dsa", "fph"}:
        transport.write("FORM ASC")
        start = number(str(adapter.read("start_frequency")))
        stop = number(str(adapter.read("stop_frequency")))
        values = numeric_values(transport.query("TRAC:DATA? TRACE1" if family == "dsa" else "TRAC1:DATA?"))
        amplitude_unit = transport.query("UNIT:POW?").decode("ascii").strip()
        if values.size == 0 or stop < start:
            raise DeviceError("Invalid spectrum axis", "parse_error")
        if stop == start:
            duration = number(str(adapter.read("sweep_time")))
            axis, unit = np.linspace(0, duration, values.size), "s"
        else:
            axis, unit = np.linspace(start, stop, values.size), "Hz"
        result.append(
            Measurement("spectrum", amplitude_unit, values, axis=axis, axis_unit=unit, metadata={"trace": 1})
        )
    elif family == "vna":
        transport.write("FORM ASC")
        result.append(complex_trace(transport.query("CALC1:DATA? SDAT"), transport.query("CALC1:DATA:STIM?")))
        adapter.configuration["correction_status"] = adapter.read("correction_status")
        adapter.configuration["trace_catalog"] = adapter.read("trace_catalog")
        adapter.configuration["selected_trace"] = adapter.read("selected_trace")
    elif family == "dmm":
        transport.write("OFORMAT ASCII")
        function = adapter.read("function_status")
        if request.initiate:
            transport.write("TARM HOLD")
            transport.write("MEM OFF")
            transport.write(f"NRDGS {request.points},AUTO")
            transport.write("TRIG AUTO")
            transport.write("TARM SGL")
        else:
            transport.write(f"RMEM 1,{request.points}")
        parts, size = [], 0
        deadline = time.monotonic() + adapter.config.timeout
        while size < request.points:
            if time.monotonic() > deadline:
                raise DeviceError("3458A readings timed out", "timeout")
            part = numeric_values(transport.read())
            if not part.size or size + part.size > request.points:
                raise DeviceError("3458A reading count mismatch", "parse_error")
            parts.append(part)
            size += part.size
        values = np.concatenate(parts)
        # FUNC? is a numeric function code on 3458A. Keep raw code; never label unknown units as volts.
        # 03458-90014 chapter 6 FUNC pp.183-185: response is function,actual_range;
        # QFORMAT ALPHA can prepend FUNC and return a symbolic function.
        units = {
            "DCV": "V",
            "ACV": "V",
            "ACDCV": "V",
            "DCI": "A",
            "ACI": "A",
            "ACDCI": "A",
            "OHM": "ohm",
            "OHMF": "ohm",
            "FREQ": "Hz",
            "PER": "s",
        }
        field = re.sub(r"^FUNC\s+", "", str(function).upper()).split(",")[0].strip()
        codes = {
            1: "DCV",
            2: "ACV",
            3: "ACDCV",
            4: "OHM",
            5: "OHMF",
            6: "DCI",
            7: "ACI",
            8: "ACDCI",
            9: "FREQ",
            10: "PER",
            11: "DCV",
            12: "DCV",
            13: "DCV",
            14: "DCV",
        }
        try:
            field = codes.get(int(float(field)), field)
        except ValueError:
            pass
        unit = units.get(field, "")
        quality = Quality.OVERLOAD if np.any(np.abs(values) >= 1e37) else Quality.GOOD
        result.append(
            Measurement("reading", unit, values, quality=quality, metadata={"function_code": function})
        )
    return tuple(result)
