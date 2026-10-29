"""Stateful synthetic counter. No hardware addresses or captured asset identities."""

import struct

from lab_control.domain.instruments import DeviceError
from lab_control.instruments.devices.keysight_53220a.adapter import GLOBAL_STATE


class Counter:
    resource = "mock://counter"

    def __init__(self):
        self.history = []
        self.state = dict.fromkeys(GLOBAL_STATE, "0")
        self.state.update(
            {
                "SENS:FUNC": "FREQ 1",
                "SENS:FREQ:MODE": "AUTO",
                "SENS:FREQ:GATE:TIME": "0.1",
                "SENS:FREQ:GATE:SOUR": "TIME",
                "SYST:TIM": "1",
                "FORM:DATA": "ASC,15",
                "FORM:BORD": "NORM",
                "TRIG:SOUR": "IMM",
                "TRIG:COUN": "1",
                "SAMP:COUN": "1",
                "SENS:ROSC:SOUR": "INT",
                "SENS:ROSC:SOUR:AUTO": "1",
                "SENS:ROSC:EXT:FREQ": "10000000",
                "OUTP:STAT": "0",
                "SYST:LANG": "53220A",
            }
        )
        for ch in (1, 2):
            for key, value in {
                "PROB": "1",
                "RANG": "5",
                "IMP": "1000000",
                "COUP": "AC",
                "FILT": "0",
                "NREJ": "0",
                "SLOP": "POS",
                "LEV:REL": "50",
                "LEV": "0",
                "LEV:AUTO": "0",
                "PROT": "0",
            }.items():
                self.state[f"INP{ch}:{key}"] = value
        self.error = '0,"No error"'
        self.measurement = 100e6
        self.failure = None
        self.during_read = None
        self.closed = False

    def write(self, command):
        self.history.append(command)
        if self.failure and command == self.failure:
            raise DeviceError("synthetic timeout", "timeout")
        if command.startswith("SENS:FUNC "):
            self.state["SENS:FUNC"] = command.split('"')[1]
            for key in self.state:
                if key.startswith("CALC") and key.endswith("STAT"):
                    self.state[key] = "0"
        elif command not in {"ABOR", "INIT:IMM"}:
            key, value = command.split(" ", 1)
            assert key in self.state, command
            self.state[key] = {"ON": "1", "OFF": "0"}.get(value, value)

    def query(self, command):
        self.history.append(command)
        if command == "*IDN?":
            return b"Agilent Technologies,53220A,TEST-COUNTER,03.00"
        if command == "*OPT?":
            return b"0"
        if command == "*OPC?":
            return b"1"
        if command == "SYST:ERR?":
            error, self.error = self.error, '0,"No error"'
            return error.encode()
        assert command.endswith("?"), command
        return self.state[command[:-1]].encode()

    def query_binary_timeout(self, command, max_bytes, seconds):
        self.history.append(command)
        assert max_bytes == 8 and 5 < seconds <= 3010
        if self.failure == command:
            raise DeviceError("synthetic timeout", "timeout")
        if self.during_read:
            self.during_read()
        byte_order = ">" if self.state["FORM:BORD"] == "NORM" else "<"
        return b"#18" + struct.pack(byte_order + "d", self.measurement) + b"\n"

    def close(self):
        self.closed = True
