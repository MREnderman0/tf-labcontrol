from __future__ import annotations

import socket
import struct
import threading
from typing import Any

from lab_control.domain.instruments import DeviceError
from lab_control.transport.base import MAX_MESSAGE, BufferedReader


class TcpSocketTransport:
    def __init__(self, host: str, port: int = 5025, timeout: float = 3, terminator: bytes = b"\n") -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.resource = f"tcp://{host}:{port}"
        self.terminator = terminator
        self.sock: socket.socket | None = None
        self.reader = BufferedReader(self._receive, terminator)
        self.lock = threading.RLock()

    def open(self) -> None:
        try:
            self.sock = socket.create_connection((self.host, self.port), self.timeout)
            self.sock.settimeout(self.timeout)
            self.reader.buffer.clear()
        except OSError as exc:
            raise DeviceError(str(exc), "connection_failed") from exc

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def _receive(self, count: int) -> bytes:
        if self.sock is None:
            raise DeviceError("TCP session closed", "disconnected")
        try:
            return self.sock.recv(count)
        except TimeoutError as exc:
            raise DeviceError("TCP read timeout", "timeout") from exc
        except OSError as exc:
            raise DeviceError(str(exc), "disconnected") from exc

    def _send(self, data: bytes) -> None:
        if self.sock is None:
            raise DeviceError("TCP session closed", "disconnected")
        try:
            self.sock.sendall(data)
        except TimeoutError as exc:
            raise DeviceError("TCP write timeout; result uncertain", "timeout") from exc
        except OSError as exc:
            raise DeviceError(str(exc), "disconnected") from exc

    def write(self, payload: bytes | str) -> None:
        data = payload.encode("ascii") + self.terminator if isinstance(payload, str) else payload
        self._send(data)

    def read(self) -> bytes:
        return self.reader.message()

    def query(self, payload: bytes | str) -> bytes:
        with self.lock:
            self.write(payload)
            return self.read()

    def query_fixed(self, payload: bytes | str, count: int) -> bytes:
        with self.lock:
            self.write(payload)
            return self.reader.exact(count)


class VicpTransport(TcpSocketTransport):
    """LeCroy VICP DATA/EOI packets, including segmented responses (8-byte header)."""

    def __init__(self, host: str, port: int = 1861, timeout: float = 3) -> None:
        super().__init__(host, port, timeout)
        self.resource = f"vicp://{host}:{port}"
        self.sequence = 0

    def write(self, payload: bytes | str) -> None:
        data = payload.encode("ascii") + b"\n" if isinstance(payload, str) else payload
        self.sequence = self.sequence % 255 + 1
        self._send(struct.pack(">BBBBI", 0x81, 1, self.sequence, 0, len(data)) + data)

    def read(self) -> bytes:
        data = bytearray()
        while True:
            flags, version, _seq, reserved, length = struct.unpack(">BBBBI", self.reader.exact(8))
            if version != 1 or reserved != 0 or length > MAX_MESSAGE - len(data):
                raise DeviceError("Invalid VICP header", "parse_error")
            segment = self.reader.exact(length)
            if flags & 0x80:
                data.extend(segment)
            if flags & 0x01:
                return bytes(data)


class SerialTransport:
    def __init__(
        self, port: str, baud_rate: int = 9600, timeout: float = 3, terminator: bytes = b"\n", **settings: Any
    ) -> None:
        self.resource, self.port = port.upper(), port
        self.baud_rate, self.timeout, self.terminator = baud_rate, timeout, terminator
        self.settings = settings
        self.handle: Any = None
        self.reader = BufferedReader(self._receive, terminator)

    def open(self) -> None:
        try:
            import serial

            # Caller must opt into a known serial profile; discovery never opens COM ports.
            self.handle = serial.Serial(
                port=None,
                baudrate=self.baud_rate,
                timeout=self.timeout,
                write_timeout=self.timeout,
                **self.settings,
            )
            self.handle.dtr = False
            self.handle.rts = False
            self.handle.port = self.port
            self.handle.open()
            self.reader.buffer.clear()
        except Exception as exc:
            self.close()
            raise DeviceError(f"Serial open: {exc}", "connection_failed") from exc

    def close(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None

    def _receive(self, count: int) -> bytes:
        try:
            chunk = self.handle.read(count)
            if not chunk:
                raise DeviceError("Serial read timeout", "timeout")
            return bytes(chunk)
        except DeviceError:
            raise
        except Exception as exc:
            raise DeviceError(str(exc), "disconnected") from exc

    def write(self, payload: bytes | str) -> None:
        data = payload.encode("ascii") if isinstance(payload, str) else payload
        try:
            expected = data + self.terminator if isinstance(payload, str) else data
            if self.handle.write(expected) != len(expected):
                raise DeviceError("Short serial write; result uncertain", "timeout")
        except DeviceError:
            raise
        except Exception as exc:
            raise DeviceError(str(exc), "disconnected") from exc

    def query_fixed(self, payload: bytes | str, count: int) -> bytes:
        self.write(payload)
        return self.reader.exact(count)

    def read(self) -> bytes:
        return self.reader.message()

    def query(self, payload: bytes | str) -> bytes:
        self.write(payload)
        return self.read()
