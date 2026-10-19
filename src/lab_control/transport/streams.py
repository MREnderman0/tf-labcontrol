from __future__ import annotations

import socket
import struct
import threading
from typing import Any

from lab_control.domain.instruments import DeviceError
from lab_control.transport.base import MAX_MESSAGE, BufferedReader
from lab_control.transport.diagnostics import measured, synchronized


class TcpSocketTransport:
    def __init__(
        self,
        host: str,
        port: int = 5025,
        timeout: float = 3,
        terminator: bytes = b"\n",
        source_address: str = "",
    ) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.source_address = source_address
        self.resource = f"tcp://{host}:{port}"
        self.terminator = terminator
        self.sock: socket.socket | None = None
        self.reader = BufferedReader(self._receive, terminator, self._receive_some)
        self.lock = threading.RLock()

    def open(self) -> None:
        try:
            if self.source_address:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.settimeout(self.timeout)
                self.sock.bind((self.source_address, 0))
                self.sock.connect((self.host, self.port))
            else:
                self.sock = socket.create_connection((self.host, self.port), self.timeout)
            self.sock.settimeout(self.timeout)
            # SCPI/VICP exchanges are latency-sensitive, small request/reply
            # messages. Do not coalesce a later command behind an earlier ACK.
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.reader.buffer.clear()
        except OSError as exc:
            self.close()
            raise DeviceError(str(exc), "connection_failed") from exc

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def _receive_some(self, count: int) -> bytes:
        return self._receive(count)

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

    @synchronized
    @measured("write")
    def write(self, payload: bytes | str) -> None:
        data = payload.encode("ascii") + self.terminator if isinstance(payload, str) else payload
        self._send(data)

    @synchronized
    @measured("read")
    def read(self) -> bytes:
        return self.reader.message()

    @synchronized
    @measured("query")
    def query(self, payload: bytes | str) -> bytes:
        with self.lock:
            self.write(payload)
            return self.read()

    @synchronized
    @measured("query")
    def query_fixed(self, payload: bytes | str, count: int) -> bytes:
        with self.lock:
            self.write(payload)
            return self._read_exact(count)

    @measured("read")
    def _read_exact(self, count: int) -> bytes:
        return self.reader.exact(count)


class ScpiSocketTransport(TcpSocketTransport):
    """Raw SCPI socket transactions; keep VICP's framed protocol separate."""

    @synchronized
    @measured("query")
    def query_binary(self, payload: bytes | str, max_bytes: int) -> bytes:
        if not 0 < max_bytes <= MAX_MESSAGE:
            raise DeviceError("Invalid binary receive budget", "invalid_parameter")
        if self.sock is None:
            raise DeviceError("Binary connection requires explicit reconnect", "disconnected")
        try:
            self.write(payload)
            return self.reader.definite_block(max_bytes)
        except Exception:
            # A partial binary transaction cannot be safely reused or replayed.
            self.close()
            raise

    @synchronized
    @measured("query")
    def query_binary_flexible(self, payload: bytes | str, payload_bytes: int) -> bytes:
        """Read a known-size raw payload or an IEEE block without guessing by timeout.

        A few model-specific SOCKET protocols return fixed binary data directly,
        while another command on the same session uses an IEEE block.  The
        caller supplies the qualified payload size; a leading ``#`` selects the
        validated block parser, otherwise the exact payload and line trailer
        are consumed.
        """
        if not 1 <= payload_bytes <= MAX_MESSAGE:
            raise DeviceError("Invalid binary payload size", "invalid_parameter")
        if self.sock is None:
            raise DeviceError("Binary connection requires explicit reconnect", "disconnected")
        try:
            self.write(payload)
            first = self.reader.exact(1)
            if first == b"#":
                self.reader.buffer[:0] = first
                return self.reader.definite_block(payload_bytes)
            body = first + self.reader.exact(payload_bytes - 1)
            trailer = self.reader.exact(1)
            if trailer == b"\r":
                trailer += self.reader.exact(1)
            if trailer not in (b"\n", b"\r\n"):
                raise DeviceError("Invalid raw binary trailer", "parse_error")
            return body
        except Exception:
            self.close()
            raise

    @synchronized
    def query_timeout(self, payload: bytes | str, seconds: float) -> bytes:
        if not 0.1 <= seconds <= 60:
            raise DeviceError("Invalid query timeout", "invalid_parameter")
        if self.sock is None:
            raise DeviceError("Connection requires explicit reconnect", "disconnected")
        old = self.sock.gettimeout()
        self.sock.settimeout(seconds)
        try:
            return self.query(payload)
        except Exception:
            self.close()
            raise
        finally:
            if self.sock is not None:
                self.sock.settimeout(old)


class VicpTransport(TcpSocketTransport):
    """LeCroy VICP DATA/EOI packets, including segmented responses (8-byte header)."""

    def __init__(self, host: str, port: int = 1861, timeout: float = 3) -> None:
        super().__init__(host, port, timeout)
        self.resource = f"vicp://{host}:{port}"
        self.sequence = 0

    @synchronized
    @measured("write")
    def write(self, payload: bytes | str) -> None:
        data = payload.encode("ascii") + b"\n" if isinstance(payload, str) else payload
        self.sequence = self.sequence % 255 + 1
        self._send(struct.pack(">BBBBI", 0x81, 1, self.sequence, 0, len(data)) + data)

    @synchronized
    @measured("read")
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
        self.lock = threading.RLock()
        self.handle: Any = None
        self.reader = BufferedReader(self._receive, terminator, self._receive_some)

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

    def _receive_some(self, count: int) -> bytes:
        first = self._receive(1)
        available = min(count - 1, int(self.handle.in_waiting))
        return first + (self._receive(available) if available > 0 else b"")

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

    @synchronized
    @measured("write")
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

    @synchronized
    @measured("query")
    def query_fixed(self, payload: bytes | str, count: int) -> bytes:
        self.write(payload)
        return self._read_exact(count)

    @measured("read")
    def _read_exact(self, count: int) -> bytes:
        return self.reader.exact(count)

    @synchronized
    @measured("read")
    def read_pending(self) -> bytes:
        return self.reader.exact(len(self.reader.buffer) + self.handle.in_waiting)

    @synchronized
    @measured("read")
    def read(self) -> bytes:
        return self.reader.message()

    @synchronized
    @measured("query")
    def query(self, payload: bytes | str) -> bytes:
        self.write(payload)
        return self.read()
