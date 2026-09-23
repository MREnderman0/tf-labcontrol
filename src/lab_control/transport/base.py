from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from typing import Protocol

import numpy as np

from lab_control.domain.instruments import DeviceError

MAX_MESSAGE = 64 * 1024 * 1024


class Transport(Protocol):
    resource: str

    def open(self) -> None: ...
    def close(self) -> None: ...
    def write(self, payload: bytes | str) -> None: ...
    def read(self) -> bytes: ...
    def query(self, payload: bytes | str) -> bytes: ...


def ieee_block(payload: bytes) -> bytes:
    size = str(len(payload)).encode("ascii")
    return b"#" + str(len(size)).encode("ascii") + size + payload


def block_payload(raw: bytes, *, allow_indefinite: bool = False) -> bytes:
    """Validate a complete IEEE 488.2 block; never strip binary payload bytes."""
    if len(raw) < 2 or raw[:1] != b"#" or not raw[1:2].isdigit():
        raise DeviceError("Invalid IEEE block header", "parse_error")
    digits = int(raw[1:2])
    if digits == 0:
        if not allow_indefinite:
            raise DeviceError("Indefinite block requires explicit transport EOM", "parse_error")
        return raw[2:]
    if len(raw) < 2 + digits or not raw[2 : 2 + digits].isdigit():
        raise DeviceError("Invalid IEEE block length", "parse_error")
    size = int(raw[2 : 2 + digits])
    end = 2 + digits + size
    if size > MAX_MESSAGE or len(raw) < end or raw[end:] not in (b"", b"\n", b"\r\n"):
        raise DeviceError("Truncated, excessive or trailing IEEE block data", "parse_error")
    return raw[2 + digits : end]


def numeric_values(raw: bytes, dtype: str = "<f4") -> np.ndarray:
    try:
        if raw.startswith(b"#"):
            data = block_payload(raw)
            if len(data) % np.dtype(dtype).itemsize:
                raise ValueError("unaligned payload")
            values = np.frombuffer(data, dtype=dtype).copy()
            if not np.isfinite(values).all():
                raise DeviceError("Non-finite instrument data", "overload")
            return values
        text = raw.decode("ascii").strip()
        values = np.asarray([float(v) for v in text.split(",")], dtype=float)
        if not np.isfinite(values).all():
            raise DeviceError("Non-finite instrument data", "overload")
        return values
    except (ValueError, UnicodeError) as exc:
        raise DeviceError(f"Invalid numeric response: {exc}", "parse_error") from exc


class BufferedReader:
    """Framing for byte streams. A read may return part of, or several, messages."""

    def __init__(self, receive: Callable[[int], bytes], terminator: bytes = b"\n") -> None:
        self.receive = receive
        self.terminator = terminator
        self.buffer = bytearray()

    def exact(self, count: int) -> bytes:
        if not 0 <= count <= MAX_MESSAGE:
            raise DeviceError("Excessive response length", "parse_error")
        while len(self.buffer) < count:
            chunk = self.receive(min(65536, count - len(self.buffer)))
            if not chunk:
                raise DeviceError("Connection closed during response", "disconnected")
            self.buffer.extend(chunk)
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        return result

    def message(self) -> bytes:
        first = self.exact(1)
        while first in (b"\n", b"\r"):
            first = self.exact(1)
        if first == b"#":
            digits = self.exact(1)
            if digits not in b"123456789":
                raise DeviceError("Streaming block must have a definite length", "parse_error")
            length = self.exact(int(digits))
            if not length.isdigit():
                raise DeviceError("Invalid block length", "parse_error")
            return first + digits + length + self.exact(int(length))
        data = bytearray(first)
        while not data.endswith(self.terminator):
            data.extend(self.exact(1))
            if len(data) > MAX_MESSAGE:
                raise DeviceError("Response exceeds size limit", "parse_error")
        return bytes(data)


class MockTransport:
    """Strict scripted exchanges. Unexpected I/O fails rather than inventing data."""

    def __init__(self, exchanges: list[tuple[bytes | str, bytes | Exception]] | None = None) -> None:
        self.resource = "mock://scripted"
        self.exchanges = deque(exchanges or [])
        self.history: list[bytes | str] = []
        self.open_count = 0
        self.close_count = 0
        self.opened = False
        self._reply: bytes | Exception = b""
        self.lock = threading.RLock()

    def open(self) -> None:
        self.open_count += 1
        self.opened = True

    def close(self) -> None:
        self.close_count += 1
        self.opened = False

    def write(self, payload: bytes | str) -> None:
        if not self.opened:
            raise DeviceError("Mock is closed", "disconnected")
        self.history.append(payload)
        if not self.exchanges:
            raise AssertionError(f"Unexpected command: {payload!r}")
        expected, self._reply = self.exchanges.popleft()
        if payload != expected:
            raise AssertionError(f"Expected {expected!r}, received {payload!r}")

    def read(self) -> bytes:
        if isinstance(self._reply, Exception):
            raise self._reply
        return self._reply

    def query(self, payload: bytes | str) -> bytes:
        with self.lock:
            self.write(payload)
            return self.read()
