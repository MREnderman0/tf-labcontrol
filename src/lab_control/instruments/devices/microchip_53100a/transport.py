"""Prompt-framed TSERVE command channel. No measurement is started by open()."""

import re
import time
from collections.abc import Callable

from lab_control.domain.instruments import DeviceError
from lab_control.transport.read_wait import read_timeout
from lab_control.transport.streams import TcpSocketTransport

PROMPT = re.compile(rb"=[^\r\n>]+ > ")
MAX_REPLY = 4 * 1024 * 1024


class TserveTransport(TcpSocketTransport):
    def __init__(
        self,
        host: str,
        port: int = 1299,
        timeout: float = 3,
        terminator: bytes = b"\r\n",
        *,
        trace: Callable[[dict], None] | None = None,
    ) -> None:
        super().__init__(host, port, timeout, terminator)
        self.trace = trace

    def open(self) -> None:
        super().open()
        try:
            banner = self.read()
            if self.trace:
                self.trace({"event": "welcome", "reply_hex": banner.hex()})
        except Exception:
            self.close()
            raise

    def query(self, payload: bytes | str) -> bytes:
        with self.lock:
            event = {
                "event": "query",
                "command": payload.decode("ascii") if isinstance(payload, bytes) else payload,
            }
            try:
                reply = super().query(payload)
                event["reply_hex"] = reply.hex()
                return reply
            except Exception as exc:
                event["error"] = str(exc)
                # A delayed reply cannot be allowed to become the next command's reply.
                self.close()
                raise
            finally:
                if self.trace:
                    self.trace(event)

    def read(self) -> bytes:
        data = bytearray()
        line_start = 0
        escape = False
        negotiation = 0
        deadline = time.monotonic() + read_timeout(self.timeout)
        previous = self.sock.gettimeout() if self.sock is not None else None
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DeviceError("TSERVE prompt timeout", "timeout")
                if self.sock is not None:
                    self.sock.settimeout(remaining)
                if self.reader.buffer:
                    chunk = bytes(self.reader.buffer)
                    self.reader.buffer.clear()
                else:
                    chunk = self._receive(65536)
                if not chunk:
                    raise DeviceError("Connection closed during TSERVE response", "disconnected")
                for index, byte in enumerate(chunk):
                    # A Telnet escape or option can span TCP reads.
                    if negotiation:
                        if negotiation in (251, 253):
                            self._send(bytes((255, 254 if negotiation == 251 else 252, byte)))
                        negotiation = 0
                        continue
                    if escape:
                        escape = False
                        if byte in (251, 252, 253, 254):
                            negotiation = byte
                            continue
                        if byte != 255:
                            raise DeviceError("Unsupported TSERVE Telnet negotiation", "parse_error")
                    elif byte == 255:
                        escape = True
                        continue

                    data.append(byte)
                    if byte in (13, 10):
                        line_start = len(data)
                    # Check only a possible prompt line and consume its trailing space.
                    elif byte == 32 and data[line_start] == 61 and data.endswith(b" > "):
                        if PROMPT.fullmatch(data, line_start):
                            self.reader.buffer.extend(chunk[index + 1 :])
                            return bytes(data[:line_start]).strip()
                    if len(data) >= MAX_REPLY:
                        raise DeviceError("Excessive TSERVE response", "parse_error")
        finally:
            if self.sock is not None:
                self.sock.settimeout(previous)
