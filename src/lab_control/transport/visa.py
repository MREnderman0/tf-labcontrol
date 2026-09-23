from __future__ import annotations

import threading
from typing import Any

from lab_control.domain.instruments import DeviceError
from lab_control.transport.base import BufferedReader


class VisaRuntime:
    """One owner per backend per application, shared by discovery and sessions."""

    def __init__(self, library: str = "", manager: Any = None) -> None:
        self.library = library
        self._manager = manager
        self._lock = threading.RLock()
        self._handles: set[Any] = set()

    @property
    def manager(self) -> Any:
        with self._lock:
            if self._manager is None:
                try:
                    import pyvisa

                    self._manager = pyvisa.ResourceManager(self.library)
                except Exception as exc:
                    raise DeviceError(f"VISA runtime unavailable: {exc}", "driver_missing") from exc
            return self._manager

    def enumerate(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self.manager.list_resources("?*"))

    def open(self, resource: str, **kwargs: Any) -> Any:
        with self._lock:
            handle = self.manager.open_resource(resource, **kwargs)
            self._handles.add(handle)
            return handle

    def release(self, handle: Any) -> None:
        with self._lock:
            try:
                handle.close()
            finally:
                self._handles.discard(handle)

    def close(self) -> None:
        with self._lock:
            if self._handles:
                raise DeviceError("Close instrument sessions before VISA runtime", "resource_busy")
            if self._manager is not None:
                self._manager.close()
                self._manager = None


class VisaTransport:
    def __init__(
        self,
        runtime: VisaRuntime,
        resource: str,
        timeout: float = 3,
        terminator: str = "\n",
        baud_rate: int = 9600,
        line_framing: bool = False,
    ) -> None:
        self.runtime, self.resource, self.timeout = runtime, resource, timeout
        self.terminator, self.baud_rate = terminator, baud_rate
        self.line_framing = line_framing
        self.handle: Any = None
        self.reader = BufferedReader(
            lambda count: bytes(self.handle.read_bytes(count, break_on_termchar=False)), terminator.encode()
        )

    def open(self) -> None:
        try:
            self.handle = self.runtime.open(self.resource, open_timeout=int(self.timeout * 1000))
            self.handle.timeout = int(self.timeout * 1000)
            self.handle.write_termination = self.terminator
            self.handle.read_termination = self.terminator or None
            self.reader.buffer.clear()
            if self.resource.upper().startswith("ASRL"):
                self.handle.baud_rate = self.baud_rate
        except Exception as exc:
            if self.handle is not None:
                self.close()
            raise DeviceError(f"VISA open: {exc}", "connection_failed") from exc

    def close(self) -> None:
        if self.handle is not None:
            handle, self.handle = self.handle, None
            self.runtime.release(handle)

    def _error(self, exc: Exception) -> DeviceError:
        code = "timeout" if "TMO" in str(exc).upper() or "TIMEOUT" in str(exc).upper() else "disconnected"
        return DeviceError(str(exc), code)

    def write(self, payload: bytes | str) -> None:
        try:
            if isinstance(payload, bytes):
                self.handle.write_raw(payload)
            else:
                self.handle.write(payload)
        except Exception as exc:
            raise self._error(exc) from exc

    def read(self) -> bytes:
        try:
            # Raw transfer relies on VISA END; disables termination inside binary payloads.
            old = self.handle.read_termination
            self.handle.read_termination = None
            try:
                if (
                    self.line_framing
                    or self.resource.upper().startswith("ASRL")
                    or self.resource.upper().endswith("::SOCKET")
                ):
                    return self.reader.message()
                return bytes(self.handle.read_raw())
            finally:
                self.handle.read_termination = old
        except Exception as exc:
            raise self._error(exc) from exc

    def query(self, payload: bytes | str) -> bytes:
        self.write(payload)
        return self.read()

    def query_fixed(self, payload: bytes | str, count: int) -> bytes:
        self.write(payload)
        try:
            return self.reader.exact(count)
        except Exception as exc:
            raise self._error(exc) from exc
