"""VISA ASRL setup for the pump's fixed binary frames."""

import re

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import ValidationError
from lab_control.transport.visa import VisaRuntime, VisaTransport


class PumpVisaTransport(VisaTransport):
    def open(self) -> None:
        from pyvisa.constants import ControlFlow, Parity, SerialTermination, StopBits

        super().open()
        self.handle.data_bits = 8
        self.handle.parity = Parity.none
        self.handle.stop_bits = StopBits.one
        self.handle.flow_control = ControlFlow.none
        self.handle.end_input = SerialTermination.none
        self.handle.end_output = SerialTermination.none
        self.handle.read_termination = None
        self.handle.write_termination = None


def create_transport(config: ConnectionConfig, runtime: VisaRuntime) -> PumpVisaTransport:
    if config.transport != "visa" or not re.fullmatch(r"ASRL[1-9][0-9]*::INSTR", config.resource):
        raise ValidationError("请手动选择泵浦的 Windows COM 口")
    return PumpVisaTransport(runtime, config.resource, config.timeout, terminator="", baud_rate=115200)
