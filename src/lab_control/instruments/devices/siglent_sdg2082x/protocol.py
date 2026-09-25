"""SDG2000X composite responses and lossless waveform framing (PG02-E05A)."""

from __future__ import annotations

import math
import re
import struct
from typing import Any

from lab_control.domain.instruments import DeviceError, Unsupported, ValidationError

MODES = {"AM", "FM", "PM", "DSBAM", "FSK", "ASK", "PSK", "PWM"}


def reference_state(raw: bytes) -> dict[str, str]:
    """PG02-E05A §3.20, including optional CHDR and the read-only 10MOUT field."""
    text = raw.decode("ascii").strip().upper()
    match = re.fullmatch(r"(?:ROSC(?:ILLATOR)?\s+)?(INT|EXT)(?:\s*,\s*10MOUT\s*,\s*(ON|OFF))?", text)
    if match is None:
        raise DeviceError(f"Invalid SDG reference response: {text!r}", "parse_error")
    return {"source": match[1], **({"10MOUT": match[2]} if match[2] else {})}


def reference_source(raw: bytes) -> str:
    return reference_state(raw)["source"]


def fields(raw: bytes) -> dict[str, Any]:
    text = raw.decode("ascii").strip()
    text = re.sub(r"^(?:C[12]:)?[A-Z_]+\s+", "", text)
    tokens = [s.strip() for s in text.split(",")]
    result: dict[str, Any] = {}
    target = result
    i = 0
    while i < len(tokens):
        key = tokens[i].upper()
        if key in MODES:
            result["MODE"] = key
            i += 1
        elif key == "CARR":
            target = {}
            result["CARR"] = target
            i += 1
        elif i + 1 < len(tokens):
            value: Any = tokens[i + 1]
            match = re.fullmatch(
                r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:E[+-]?\d+)?)(HZ|V|S|%|DEG)?", value.upper()
            )
            if match:
                value = float(match[1])
                if not math.isfinite(value):
                    raise DeviceError("Non-finite SDG value", "parse_error")
            if key in target:
                raise DeviceError(f"Duplicate SDG field {key}", "parse_error")
            target[key] = value
            i += 2
        else:
            raise DeviceError(f"Incomplete SDG response: {text!r}", "parse_error")
    return result


def field(raw: bytes, name: str, mode: str = "") -> Any:
    values = fields(raw)
    if mode and values.get("MODE") != mode:
        raise Unsupported(f"{mode} parameters unavailable in the current mode")
    if name not in values:
        raise Unsupported(f"SDG field {name} unavailable (mode may be OFF)")
    return values[name]


def waveform_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,23}", name):
        raise ValidationError("Waveform name: 1..24 ASCII letters/digits/underscore, starting with a letter")
    return name


def waveform_bytes(samples: list[float]) -> bytes:
    if not 8 <= len(samples) <= 131072:
        raise ValidationError("This upload implementation accepts 8..131072 samples")
    if any(not math.isfinite(x) or not -1 <= x <= 1 for x in samples):
        raise ValidationError("Samples must be finite and within -1..1")
    return struct.pack(f"<{len(samples)}h", *(round(x * 32767) for x in samples))


def parse_waveform(raw: bytes, expected_name: str) -> tuple[dict[str, Any], bytes]:
    header, separator, tail = raw.partition(b"WAVEDATA,")
    if not separator or len(header) > 4096:
        raise DeviceError("Missing SDG waveform header", "parse_error")
    meta = fields(header.rstrip(b","))
    if meta.get("WVNM") != expected_name:
        raise DeviceError("Waveform readback name mismatch", "parse_error")
    length = str(meta.get("LENGTH", ""))
    match = re.fullmatch(r"(\d+)B", length)
    if not match:
        raise DeviceError("Missing waveform byte length", "parse_error")
    count = int(match[1])
    if not 16 <= count <= 16 * 1024 * 1024 or count % 2:
        raise DeviceError("Invalid waveform byte length", "parse_error")
    data, ending = tail[:count], tail[count:]
    if len(data) != count or ending not in (b"", b"\n", b"\r\n"):
        raise DeviceError("Truncated/excess waveform payload", "parse_error")
    return meta, data


def user_waveforms(raw: bytes) -> tuple[str, ...]:
    text = raw.decode("ascii").strip()
    text = re.sub(r"^STL\s+", "", text)
    if text == "WVNM":
        return ()
    if not text.startswith("WVNM,"):
        raise DeviceError("Invalid user waveform catalog", "parse_error")
    return tuple(x for x in text[5:].split(",") if x)
