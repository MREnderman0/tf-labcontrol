"""Model-owned binary status framing and 9600-baud command pacing."""

from __future__ import annotations

import re
import time
from typing import Any

from lab_control.domain.instruments import DeviceError
from lab_control.instruments.base import number
from lab_control.transport.base import Transport

MANUAL = "https://www.gwinstek.com/en-US/products/downloadSeriesDownNew/7840/1477"
SPECIFICATIONS = "https://www.gwinstek.com.cn/product/detail/195"


def numeric_readback(raw: bytes, *, unit: str) -> float:
    text = raw.decode("ascii").strip()
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(" + unit + r")?", text)
    if not match:
        raise DeviceError(f"Invalid GPD {unit} reading: {text!r}", "parse_error")
    return number(match[1])


def decode_status(raw: bytes, *, firmware: str = "") -> dict[str, Any]:
    # The byte itself may be CR, LF or '#'; never strip or parse it as ASCII.
    if len(raw) == 1:
        value = raw[0]
        encoding = "binary_byte"
    else:
        bits = raw.rstrip(b"\r\n")
        if len(bits) != 8 or any(bit not in b"01" for bit in bits):
            raise DeviceError("GPD STATUS? requires one binary byte or eight ASCII bits", "parse_error")
        # V2.12 returns the manual's fields left-to-right, not one MSB-first integer:
        # CH1, CH2, tracking(two bits), beep, output, baud(two bits).
        value = (
            int(bits[0:1])
            | (int(bits[1:2]) << 1)
            | (int(bits[2:4], 2) << 2)
            | (int(bits[4:5]) << 4)
            | (int(bits[5:6]) << 5)
            | (int(bits[6:8], 2) << 6)
        )
        encoding = "ascii_fields"
    tracking_bits, baud_bits = (value >> 2) & 3, (value >> 6) & 3
    observed_independent = encoding == "ascii_fields" and firmware == "V2.12" and tracking_bits == 0
    return {
        "raw": value,
        "raw_hex": raw.hex(),
        "encoding": encoding,
        "ch1_mode": "CV" if value & 1 else "CC",
        "ch2_mode": "CV" if value & 2 else "CC",
        "tracking_bits": tracking_bits,
        # Verified by the current front-panel photo and STATUS? after TRACK0.
        # Keep the undocumented variant limited to the observed wire format/firmware.
        "tracking": "0" if observed_independent else {1: "0", 3: "1", 2: "2"}.get(tracking_bits),
        "tracking_source": "v2.12_panel_and_track0" if observed_independent else "manual",
        "beep_enabled": bool(value & 16),
        "output_enabled": bool(value & 32),
        "baud_bits": baud_bits,
        "baud_rate": {0: 115200, 1: 57600, 2: 9600}.get(baud_bits),
    }


class PacedTransport:
    """No I/O in construction; the owning session supplies serialization."""

    def __init__(self, inner: Transport, interval: float = 0.05):
        self.inner, self.resource, self.interval = inner, inner.resource, interval
        # USB NO/YES mode transitions take longer than ordinary register commands
        # on the verified V2.12 unit. Do not issue queries during that transition.
        self.transition_interval = 1.0
        self.last_completed = 0.0
        self.fixed_pending = False

    def open(self) -> None:
        self.inner.open()

    def close(self) -> None:
        self.inner.close()

    def prepare(self) -> None:
        delay = self.interval - (time.monotonic() - self.last_completed)
        if delay > 0:
            time.sleep(delay)
        if self.fixed_pending:
            # Consume only bytes already received BEFORE sending the next command.
            # Never skip a leading delimiter from a new binary status response.
            pending = getattr(self.inner, "read_pending", None)
            if pending:
                suffix = pending()
                if suffix.strip(b"\r\n"):
                    raise DeviceError("Unexpected bytes following GPD status response", "parse_error")
            self.fixed_pending = False

    def write(self, payload: bytes | str) -> None:
        self.prepare()
        try:
            self.inner.write(payload)
            if payload in ("REMOTE", "LOCAL"):
                time.sleep(self.transition_interval)
        finally:
            self.last_completed = time.monotonic()

    def read(self) -> bytes:
        try:
            return self.inner.read()
        finally:
            self.last_completed = time.monotonic()

    def query(self, payload: bytes | str) -> bytes:
        self.prepare()
        try:
            return self.inner.query(payload)
        finally:
            self.last_completed = time.monotonic()

    def query_fixed(self, payload: bytes | str, count: int) -> bytes:
        self.prepare()
        try:
            fixed = getattr(self.inner, "query_fixed", None)
            raw = fixed(payload, count) if fixed else self.inner.query(payload)
            self.fixed_pending = True
            return raw
        finally:
            self.last_completed = time.monotonic()

    def query_status(self) -> bytes:
        first = self.query_fixed("STATUS?", 1)
        # Wait for this response's remaining bytes before any subsequent command.
        delay = self.interval - (time.monotonic() - self.last_completed)
        if delay > 0:
            time.sleep(delay)
        pending = getattr(self.inner, "read_pending", None)
        suffix = pending() if pending else b""
        self.fixed_pending = False
        self.last_completed = time.monotonic()
        if first in (b"0", b"1") and suffix.strip(b"\r\n"):
            return first + suffix
        if suffix.strip(b"\r\n"):
            raise DeviceError("Unexpected bytes following GPD status response", "parse_error")
        return first
