"""Model-owned raw serial/VISA transport for CoreMorrow XMT frames."""

from __future__ import annotations

from typing import Any

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import DeviceError, Unsupported
from lab_control.transport.streams import SerialTransport
from lab_control.transport.visa import VisaRuntime, VisaTransport

from .protocol import read_framed


class XmtSerialTransport(SerialTransport):
    def __init__(self, port: str, baud_rate: int, timeout: float) -> None:
        try:
            import serial

            settings: dict[str, Any] = {
                "bytesize": serial.EIGHTBITS,
                "parity": serial.PARITY_NONE,
                "stopbits": serial.STOPBITS_ONE,
                "xonxoff": False,
                "rtscts": False,
                "dsrdtr": False,
            }
        except ImportError as exc:  # pragma: no cover - deployment diagnostic
            raise Unsupported(f"pyserial is required for CoreMorrow XMT: {exc}") from exc
        super().__init__(port, baud_rate, timeout, b"", **settings)

    def query_frame(self, payload: bytes) -> bytes:
        self.write(payload)
        try:
            return read_framed(self._read_exact)
        except DeviceError as exc:
            if exc.code in {"timeout", "disconnected"}:
                self.close()
            raise


class XmtVisaTransport(VisaTransport):
    def __init__(self, runtime: VisaRuntime, resource: str, baud_rate: int, timeout: float) -> None:
        super().__init__(runtime, resource, timeout, terminator="", baud_rate=baud_rate)

    def open(self) -> None:
        super().open()
        try:
            from pyvisa.constants import ControlFlow, Parity, StopBits

            self.handle.data_bits = 8
            self.handle.parity = Parity.none
            self.handle.stop_bits = StopBits.one
            self.handle.flow_control = ControlFlow.none
            self.handle.read_termination = None
            self.handle.write_termination = None
        except Exception:
            self.close()
            raise

    def query_frame(self, payload: bytes) -> bytes:
        self.write(payload)
        try:
            return read_framed(self._read_exact)
        except DeviceError as exc:
            if exc.code in {"timeout", "disconnected"}:
                self.close()
            raise


def create_transport(config: ConnectionConfig, runtime: VisaRuntime):
    if config.transport == "serial":
        return XmtSerialTransport(config.resource, config.baud_rate, config.timeout)
    if config.transport == "visa":
        return XmtVisaTransport(runtime, config.resource, config.baud_rate, config.timeout)
    raise Unsupported("CoreMorrow XMT requires transport=serial or transport=visa")

