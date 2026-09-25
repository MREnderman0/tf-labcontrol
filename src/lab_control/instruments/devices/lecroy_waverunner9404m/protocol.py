"""WaveRunner 9404M scalar and LECROY_2_x binary message validation."""

from __future__ import annotations

import hashlib
import math
import re
import struct
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from lab_control.domain.instruments import DeviceError, Measurement, Unsupported
from lab_control.transport.base import block_payload


def response(raw: bytes) -> str:
    try:
        value = raw.decode("ascii").strip()
    except UnicodeError as exc:
        raise DeviceError("Non-ASCII scalar reply", "parse_error") from exc
    if value.upper().startswith("WARNING"):
        if "CURRENT REMOTE CONTROL INTERFACE" in value.upper():
            raise DeviceError(
                "仪器远程接口与连接不匹配：请在 Utilities > Utilities Setup > Remote 选择 LXI (VXI-11)。"
                + value,
                "remote_interface_mismatch",
            )
        raise DeviceError(value, "instrument_warning")
    if not value or "\n" in value or "\r" in value:
        raise DeviceError("Empty or multiple scalar replies", "parse_error")
    return value


def value_text(raw: bytes) -> str:
    value = response(raw)
    return re.sub(r"^(?:C[1-4]:)?[A-Z][A-Z_]*\s+", "", value).strip()


def trigger_setup(raw: bytes) -> dict[str, str]:
    """Keep all TRSE qualifiers; a source edit must not reset holdoff."""
    parts = [v.strip().upper() for v in value_text(raw).split(",")]
    if len(parts) < 3 or len(parts) % 2 != 1 or not parts[0]:
        raise DeviceError("Invalid trigger setup", "parse_error")
    result = {"type": parts[0]}
    for key, value in zip(parts[1::2], parts[2::2], strict=True):
        if not key or not value or key in result:
            raise DeviceError("Invalid or duplicate trigger qualifier", "parse_error")
        result[key] = value
    if "SR" not in result:
        raise DeviceError("Missing trigger source", "parse_error")
    return result


def trigger_source(raw: bytes) -> str:
    return trigger_setup(raw)["SR"]


def grouped_scalars(raw: bytes, queries: tuple[str, ...]) -> tuple[bytes, ...]:
    """Qualified SHORT headers make field order explicit, even for equal values."""
    fields = response(raw).split(";")
    if len(fields) != len(queries):
        raise DeviceError("Grouped reply field count differs", "parse_error")
    for field, query in zip(fields, queries, strict=True):
        if not field.startswith(query.removesuffix("?") + " "):
            raise DeviceError("Grouped reply header/order differs", "parse_error")
    return tuple(field.encode("ascii") for field in fields)


def numeric(raw: bytes) -> float:
    text = value_text(raw).replace(" ", "")
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)([A-Za-z]*)", text)
    if not match:
        raise DeviceError(f"Invalid numeric reply: {text}", "parse_error")
    # MAUI command mnemonics use M for milli and MA for mega (not SI case).
    suffix = match[2].upper()
    factors = {
        "": 1.0,
        "V": 1.0,
        "S": 1.0,
        "HZ": 1.0,
        "SAMPLE": 1.0,
        "K": 1e3,
        "MA": 1e6,
        "G": 1e9,
        "M": 1e-3,
        "U": 1e-6,
        "N": 1e-9,
        "P": 1e-12,
    }
    for prefix, factor in (("MA", 1e6), ("K", 1e3), ("M", 1e-3), ("U", 1e-6), ("N", 1e-9), ("P", 1e-12)):
        for unit in ("V", "S", "HZ", "SAMPLE"):
            factors[prefix + unit] = factor
    if suffix not in factors:
        raise DeviceError(f"Unsupported numeric unit: {suffix}", "parse_error")
    value = float(match[1]) * factors[suffix]
    if not math.isfinite(value):
        raise DeviceError("Non-finite scalar reply", "parse_error")
    return value


def choice(raw: bytes, *, choices: tuple[str, ...]) -> str:
    value = value_text(raw).upper()
    if value not in choices:
        raise DeviceError(f"Unexpected enum reply: {value}", "parse_error")
    return value


def transfer_setup(raw: bytes) -> dict[str, int]:
    parts = value_text(raw).upper().split(",")
    if len(parts) != 8 or set(parts[::2]) != {"SP", "NP", "FP", "SN"}:
        raise DeviceError("Invalid WFSU reply", "parse_error")
    try:
        result = {key: int(value) for key, value in zip(parts[::2], parts[1::2], strict=True)}
    except ValueError as exc:
        raise DeviceError("Invalid WFSU number", "parse_error") from exc
    if any(value < 0 for value in result.values()):
        raise DeviceError("Negative WFSU parameter", "parse_error")
    return result


def waveform(raw: bytes, channel: int) -> Measurement:
    """Validate the entire definite block before exposing any samples."""
    marker = raw.find(b"#")
    if marker < 0:
        response(raw)  # Preserve interface-mismatch diagnosis for text errors.
        raise DeviceError("Missing waveform block", "parse_error")
    prefix = raw[:marker].strip()
    if prefix and not re.fullmatch(rb"(?:C[1-4]:)?(?:WF|WAVEFORM)\s+ALL,?", prefix):
        raise DeviceError("Unexpected waveform response prefix", "parse_error")
    if prefix.startswith(b"C") and not prefix.startswith(f"C{channel}:".encode()):
        raise DeviceError("Waveform response belongs to another channel", "parse_error")
    data = block_payload(raw[marker:])
    if len(data) < 346 or data[:16].rstrip(b"\0") != b"WAVEDESC":
        raise DeviceError("Missing/truncated WAVEDESC", "parse_error")
    template = data[16:32].rstrip(b"\0")
    if template not in (b"LECROY_2_3", b"LECROY_2_4"):
        raise Unsupported(f"Unqualified waveform template: {template!r}")
    endian = "<" if data[34:36] == b"\x01\x00" else ">" if data[34:36] == b"\0\0" else ""
    if not endian:
        raise DeviceError("Invalid waveform byte order", "parse_error")

    def field(fmt: str, offset: int) -> Any:
        return struct.unpack_from(endian + fmt, data, offset)[0]

    width = field("h", 32)
    lengths = [field("i", offset) for offset in range(36, 76, 4)]
    count, first, last = (field("i", offset) for offset in (116, 124, 128))
    if width not in (0, 1) or min(lengths) < 0 or lengths[0] < 346:
        raise DeviceError("Invalid waveform descriptor lengths", "parse_error")
    if sum(lengths) != len(data) or not 1 <= count <= 10_000_000:
        raise DeviceError("Waveform block length/point count mismatch", "parse_error")
    if lengths[6] != count * (width + 1) or not 0 <= first <= last < count:
        raise DeviceError("Invalid waveform sample array/valid point range", "parse_error")
    if field("i", 144) > 1 or lengths[3] or lengths[4]:
        raise Unsupported(
            "Sequence/RIS waveforms require additional timing; only single sweeps are supported"
        )
    if any(lengths[i] for i in (2, 5, 7, 8, 9)) or field("h", 316) != 0:
        raise Unsupported("Extended, paired, processed or non-single-sweep waveform")
    if field("h", 318) not in (0, 1, 2, 3) or field("h", 344) != channel - 1:
        raise Unsupported("Invalid processing state or waveform source")
    first_point, sparsing = field("i", 132), field("i", 136)
    # This qualified path preserves the complete unsparsed waveform. Do not
    # guess whether HORIZ_INTERVAL was adjusted for an arbitrary transfer window.
    if first_point != 0 or sparsing not in (0, 1):
        raise Unsupported("Offset/sparsed waveform transfer has not been qualified")
    gain, offset, dt, origin = (
        field(fmt, pos) for fmt, pos in (("f", 156), ("f", 160), ("f", 176), ("d", 180))
    )
    if not all(math.isfinite(x) for x in (gain, offset, dt, origin)) or gain <= 0 or dt <= 0:
        raise DeviceError("Invalid waveform scaling/time increment", "parse_error")
    try:
        unit = data[196:244].split(b"\0")[0].decode("ascii")
        time_unit = data[244:292].split(b"\0")[0].decode("ascii")
    except UnicodeError as exc:
        raise DeviceError("Invalid waveform units", "parse_error") from exc
    if not unit or time_unit.lower() not in ("s", "seconds"):
        raise DeviceError("Missing/unsupported waveform units", "parse_error")
    probe, vernier, acquisition_offset = (field("f", pos) for pos in (328, 336, 340))
    if not all(math.isfinite(v) for v in (probe, vernier, acquisition_offset)) or probe <= 0 or vernier <= 0:
        raise DeviceError("Invalid descriptor probe/vernier/offset", "parse_error")
    token = None
    if any(data[296:310]):
        seconds = field("d", 296)
        try:
            if not math.isfinite(seconds) or not 0 <= seconds < 60:
                raise ValueError("Invalid seconds")
            date = datetime(field("h", 308), data[307], data[306], data[305], data[304])
            token = date.isoformat() + ":" + seconds.hex()
            trigger_time = (date + timedelta(seconds=seconds)).isoformat()
        except (ValueError, OverflowError) as exc:
            raise DeviceError("Invalid descriptor trigger time", "parse_error") from exc
    else:
        trigger_time = None
    start = sum(lengths[:6])
    samples = np.frombuffer(data[start : start + lengths[6]], dtype=endian + ("i2" if width else "i1"))
    y = samples[first : last + 1].astype(np.float64) * gain - offset
    x = np.arange(first, last + 1, dtype=np.float64) * dt + origin
    if not np.isfinite(y).all() or not np.isfinite(x).all() or (len(x) > 1 and not np.all(np.diff(x) > 0)):
        raise DeviceError("Non-finite samples or non-increasing waveform axis", "parse_error")
    return Measurement(
        "waveform",
        unit,
        y,
        channel,
        x,
        "s",
        metadata={
            "template": template.decode(),
            "byte_order": "LO" if endian == "<" else "HI",
            "sample_width": width + 1,
            "descriptor_length": lengths[0],
            "raw_point_count": count,
            "first_valid": first,
            "last_valid": last,
            "gain": gain,
            "offset": offset,
            "first_point": first_point,
            "sparsing_factor": sparsing,
            "horizontal_interval": dt,
            "horizontal_offset": origin,
            "timebase_code": field("h", 324),
            "coupling_code": field("h", 326),
            "probe_attenuation": probe,
            "vertical_gain_code": field("h", 332),
            "bandwidth_limit_code": field("h", 334),
            "vertical_vernier": vernier,
            "acquisition_vertical_offset": acquisition_offset,
            "processing_code": field("h", 318),
            "acquisition_key": token,
            "trigger_time_local": trigger_time,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "trigger_time_bytes": data[296:312].hex(),
            "sequential_channel_read": True,
        },
    )
