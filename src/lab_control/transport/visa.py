from __future__ import annotations

import threading
from typing import Any

from lab_control.domain.instruments import DeviceError
from lab_control.transport.base import MAX_MESSAGE, BufferedReader, block_payload
from lab_control.transport.diagnostics import measured, synchronized


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

    def discover_lan(self, timeout: float = 3.0) -> tuple[str, ...]:
        """Use R&S documented resource-manager extensions, restoring both attributes.

        R&S visa.h: 0x0FAF0001 timeout (ms), 0x0FAF0002 mode.
        Mode 6 requests VXI-11 and mDNS, excluding static/cached resources.
        The backend enumerates all NICs; the LAN provider filters identity probes.
        """
        import ctypes

        with self._lock:
            manager = self.manager
            try:
                library = manager.visalib.lib
                saved = {}
                for attribute in (0x0FAF0001, 0x0FAF0002):
                    value = ctypes.c_size_t()
                    library.viGetAttribute(manager.session, attribute, ctypes.byref(value))
                    saved[attribute] = value.value
            except Exception as exc:
                raise DeviceError(
                    "Installed VISA backend has no R&S LAN discovery extension", "unsupported"
                ) from exc
            try:
                manager.visalib.set_attribute(manager.session, 0x0FAF0001, int(timeout * 1000))
                manager.visalib.set_attribute(manager.session, 0x0FAF0002, 6)
                return tuple(manager.list_resources("TCPIP?*::INSTR"))
            finally:
                failures = []
                for attribute, original_value in saved.items():
                    try:
                        manager.visalib.set_attribute(manager.session, attribute, original_value)
                    except Exception as exc:
                        failures.append(f"{attribute:#x}: {exc}")
                if failures:
                    raise DeviceError(
                        "LAN discovery settings restoration failed: " + "; ".join(failures),
                        "restoration_failed",
                    )

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
        self.lock = threading.RLock()
        self.handle: Any = None
        self.reader = BufferedReader(
            lambda count: bytes(self.handle.read_bytes(count, break_on_termchar=False)),
            terminator.encode(),
            self._read_text_some,
        )

    def _read_text_some(self, count: int) -> bytes:
        # viRead ends at TERMCHAR or END, unlike read_bytes(count), which loops
        # until count bytes. Binary payloads never pass through this callback.
        if not hasattr(self.handle, "visalib"):
            # Older/custom handles expose only exact reads; never ask them to
            # fill a chunk. Native VISA handles use the terminated path below.
            return bytes(self.handle.read_bytes(1, break_on_termchar=False))
        self.handle.read_termination = self.terminator
        try:
            return bytes(self.handle.visalib.read(self.handle.session, count)[0])
        finally:
            self.handle.read_termination = None

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
            code = exc.code if isinstance(exc, DeviceError) else "connection_failed"
            raise DeviceError(f"VISA open: {exc}", code) from exc

    def close(self) -> None:
        if self.handle is not None:
            handle, self.handle = self.handle, None
            self.runtime.release(handle)

    def _error(self, exc: Exception) -> DeviceError:
        if isinstance(exc, DeviceError):
            return exc
        code = "timeout" if "TMO" in str(exc).upper() or "TIMEOUT" in str(exc).upper() else "disconnected"
        return DeviceError(str(exc), code)

    @synchronized
    @measured("write")
    def write(self, payload: bytes | str) -> None:
        try:
            if isinstance(payload, bytes):
                self.handle.write_raw(payload)
            else:
                self.handle.write(payload)
        except Exception as exc:
            raise self._error(exc) from exc

    @synchronized
    @measured("read")
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

    @synchronized
    @measured("query")
    def query(self, payload: bytes | str) -> bytes:
        self.write(payload)
        return self.read()

    @synchronized
    def query_timeout(self, payload: bytes | str, seconds: float) -> bytes:
        """One query with a bounded temporary deadline; never a retry."""
        if not 0.1 <= seconds <= 60:
            raise DeviceError("Invalid query timeout", "invalid_parameter")
        if self.handle is None:
            raise DeviceError("Connection requires explicit reconnect", "disconnected")
        old = self.handle.timeout
        self.handle.timeout = int(seconds * 1000)
        try:
            return self.query(payload)
        except DeviceError:
            # A delayed completion reply must not contaminate the next command.
            self.close()
            raise
        finally:
            if self.handle is not None:
                self.handle.timeout = old

    @synchronized
    @measured("query")
    def query_binary(self, payload: bytes | str, max_bytes: int) -> bytes:
        """Bounded definite block with native VISA END or a SOCKET LF/CRLF trailer.

        An interrupted block poisons framing: close the handle, never flush/retry.
        Other transports retain their existing model-qualified acquisition path.
        """
        if not 0 < max_bytes <= MAX_MESSAGE:
            raise DeviceError("Invalid binary receive budget", "invalid_parameter")
        if self.resource.upper().endswith("::SOCKET"):
            if self.handle is None:
                raise DeviceError("Binary connection requires explicit reconnect", "disconnected")
            self.write(payload)
            old = self.handle.read_termination
            self.handle.read_termination = None
            try:
                return self.reader.definite_block(max_bytes)
            except Exception as exc:
                error = self._error(exc)
                self.close()
                raise DeviceError(f"{error}; 连接已关闭，须显式重连（未重发命令）", error.code) from exc
            finally:
                if self.handle is not None:
                    self.handle.read_termination = old
        if self.line_framing or self.resource.upper().startswith("ASRL"):
            raise DeviceError("Bounded binary requires native VISA END", "unsupported")
        if self.handle is None:
            raise DeviceError("Binary connection requires explicit reconnect", "disconnected")
        self.write(payload)
        return self._read_binary(max_bytes)

    @synchronized
    @measured("query")
    def query_binary_flexible(self, payload: bytes | str, payload_bytes: int) -> bytes:
        """Read a qualified raw payload or an IEEE block from a SOCKET resource."""
        if not 1 <= payload_bytes <= MAX_MESSAGE:
            raise DeviceError("Invalid binary payload size", "invalid_parameter")
        if self.handle is None:
            raise DeviceError("Binary connection requires explicit reconnect", "disconnected")
        old = self.handle.read_termination
        try:
            self.write(payload)
            self.handle.read_termination = None
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
        except Exception as exc:
            error = self._error(exc)
            self.close()
            raise error from exc
        finally:
            if self.handle is not None:
                self.handle.read_termination = old

    @measured("read")
    def _read_binary(self, max_bytes: int) -> bytes:
        from pyvisa.constants import StatusCode

        data = bytearray()
        declared = None
        end = None
        offset = None
        old = self.handle.read_termination
        self.handle.read_termination = None
        try:
            while True:
                # The first read takes only a bounded header, including optional
                # mnemonic prefix. No multi-megabyte allocation precedes validation.
                count = 64 if end is None else min(65536, max(1, end + 2 - len(data)))
                chunk, status = self.handle.visalib.read(self.handle.session, count)
                data.extend(chunk)
                if offset is None:
                    found = data.find(b"#")
                    if found >= 0:
                        offset = found
                    elif len(data) >= 64 or status != StatusCode.success_max_count_read:
                        raise DeviceError("Missing binary block header", "parse_error")
                if offset is not None and end is None and len(data) >= offset + 2:
                    digit = data[offset + 1 : offset + 2]
                    if digit not in b"123456789":
                        raise DeviceError("Invalid definite block header", "parse_error")
                    digits = int(digit)
                    if len(data) >= offset + 2 + digits:
                        length = data[offset + 2 : offset + 2 + digits]
                        if not length.isdigit():
                            raise DeviceError("Invalid binary declared length", "parse_error")
                        declared = int(length)
                        if declared > max_bytes:
                            raise DeviceError("IEEE block exceeds size limit", "parse_error")
                        end = offset + 2 + digits + declared
                if end is not None and len(data) > end + 2:
                    raise DeviceError("Trailing IEEE block data", "parse_error")
                if status != StatusCode.success_max_count_read:
                    if end is None or len(data) < end:
                        raise DeviceError("Truncated IEEE block", "parse_error")
                    assert offset is not None
                    block_payload(bytes(data[offset:]))
                    return bytes(data)
                if not chunk or (end is not None and len(data) >= end + 2):
                    raise DeviceError("Binary reply has no confirmed END boundary", "parse_error")
        except Exception as exc:
            error = self._error(exc)
            self.close()
            raise DeviceError(
                f"{error}; declared={declared}, received={len(data)}, limit={max_bytes}; "
                "连接已关闭，须显式重连（未重发命令）",
                error.code,
            ) from exc
        finally:
            if self.handle is not None:
                self.handle.read_termination = old

    @synchronized
    @measured("query")
    def query_fixed(self, payload: bytes | str, count: int) -> bytes:
        self.write(payload)
        try:
            old = self.handle.read_termination
            self.handle.read_termination = None
            try:
                return self._read_exact(count)
            finally:
                self.handle.read_termination = old
        except Exception as exc:
            raise self._error(exc) from exc

    @measured("read")
    def _read_exact(self, count: int) -> bytes:
        return self.reader.exact(count)

    @synchronized
    @measured("read")
    def read_pending(self) -> bytes:
        """Read already buffered ASRL bytes without waiting for another response."""
        try:
            count = int(self.handle.bytes_in_buffer)
            buffered = len(self.reader.buffer)
            old = self.handle.read_termination
            self.handle.read_termination = None
            try:
                return self.reader.exact(buffered + count)
            finally:
                self.handle.read_termination = old
        except Exception as exc:
            raise self._error(exc) from exc
