"""Prompt-framed TSERVE command channel. No measurement is started by open()."""

import re
import time
from collections.abc import Callable

from lab_control.domain.instruments import DeviceError
from lab_control.transport.streams import TcpSocketTransport


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
        deadline = time.monotonic() + self.timeout
        previous = self.sock.gettimeout() if self.sock is not None else None
        try:
            while len(data) < 4 * 1024 * 1024:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DeviceError("TSERVE prompt timeout", "timeout")
                if self.sock is not None:
                    self.sock.settimeout(remaining)
                char = self.reader.exact(1)
                if char == b"\xff":
                    verb = self.reader.exact(1)[0]
                    if verb in (251, 252, 253, 254):
                        option = self.reader.exact(1)
                        if verb in (251, 253):
                            self._send(bytes([255, 254 if verb == 251 else 252]) + option)
                    elif verb == 255:
                        data.extend(char)
                    else:
                        raise DeviceError("Unsupported TSERVE Telnet negotiation", "parse_error")
                    continue
                data.extend(char)
                # The trailing space belongs to the documented TSERVE prompt.
                # Leaving it pending breaks the next command's empty reply and
                # can reset TCP on close while unread bytes remain.
                if char == b" " and re.search(rb"(?:^|[\r\n])=[^\r\n>]+ > $", data):
                    return bytes(data[: data.rfind(b"=")]).strip()
            raise DeviceError("Excessive TSERVE response", "parse_error")
        finally:
            if self.sock is not None:
                self.sock.settimeout(previous)
