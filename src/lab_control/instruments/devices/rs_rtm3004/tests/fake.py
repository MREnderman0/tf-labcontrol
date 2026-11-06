"""Synthetic RTM3004 command peer; never opens a real connection."""

import numpy as np

from lab_control.domain.instruments import DeviceError
from lab_control.transport.base import ieee_block

from ..profile import rtm3004


class Peer:
    resource = "mock://rtm-auto"

    def __init__(self):
        self.profile = rtm3004()
        self.history = []
        self.timeouts = []
        self.closed = False
        self.auto_channels = (1, 2, 3)
        self.empty_channels = set()
        self.auto_error = '0,"No error"'
        self.error = '0,"No error"'
        self.completion = b"1"
        self.fail = ""
        self.entered = self.release = None
        defaults = {
            "channel_enabled": True,
            "scale": 0.1,
            "offset": 0.0,
            "vertical_position": 0.0,
            "coupling": "DCL",
            "probe_attenuation": 1.0,
            "bandwidth_limit": "FULL",
            "polarity": "NORM",
            "trigger_level": 0.0,
            "timebase": 0.001,
            "time_position": 0.0,
            "time_reference": 50.0,
            "horizontal_divisions": 12,
            "record_length": 10000,
            "record_length_auto": True,
            "sample_rate": 1e6,
            "trigger_source": "CH1",
            "trigger_sweep": "AUTO",
            "trigger_type": "EDGE",
            "trigger_slope": "POS",
            "acquisition_mode": "REFR",
            "decimation": "SAMP",
            "averages": 2,
            "acquisition_count": 1,
            "acquisition_state": "COMP",
            "transfer_format": "REAL,32",
            "byte_order": "LSBF",
        }
        self.values = {}
        self.queries = {}
        for name, spec in self.profile.commands.items():
            for ch in self.profile.channels if spec.capability.channel_scoped else (1,):
                if name in defaults:
                    self.values[ch, name] = defaults[name]
                    self.queries[spec.query.format(ch=ch)] = (ch, name)
        self.bounds = {
            "scale": (0.0005, 10),
            "offset": (-25, 25),
            "trigger_level": (-1, 1),
            "timebase": (4e-8, 500),
            "time_position": (-1, 100),
            "record_length": (1258, 40000000),
        }

    def open(self):
        self.closed = False

    def close(self):
        self.closed = True

    def write(self, command):
        self.history.append(command)
        if command == self.fail:
            raise DeviceError("Synthetic write timeout", "timeout")
        if command == "AUT":
            self.values[1, "timebase"] = 0.0002
            self.values[1, "time_position"] = 0.0
            self.values[1, "acquisition_state"] = "RUN"
            self.values[1, "trigger_sweep"] = "AUTO"
            self.values[1, "trigger_type"] = "EDGE"
            for ch in range(1, 5):
                self.values[ch, "scale"] = 0.2
                self.values[ch, "offset"] = 0.05
                self.values[ch, "vertical_position"] = ch - 2
                self.values[ch, "channel_enabled"] = ch in self.auto_channels
            self.error = self.auto_error
        elif command.startswith("FORM:BORD "):
            self.values[1, "byte_order"] = command.split(" ", 1)[1]
        elif command.startswith("FORM "):
            self.values[1, "transfer_format"] = command.split(" ", 1)[1]
        elif command in {"RUN", "STOP"}:
            self.values[1, "acquisition_state"] = "RUN" if command == "RUN" else "BRE"
            if command == "RUN":
                self.empty_channels.clear()
        elif command.startswith("TRIG:A:MODE "):
            self.values[1, "trigger_sweep"] = command.split(" ", 1)[1]
        else:
            raise AssertionError(command)

    def query_timeout(self, command, seconds):
        self.timeouts.append((command, seconds))
        try:
            return self.query(command)
        except DeviceError:
            self.close()
            raise

    def query(self, command):
        self.history.append(command)
        if self.closed:
            raise DeviceError("Connection closed", "disconnected")
        if command == self.fail:
            raise DeviceError("Synthetic read timeout", "timeout")
        if command == "*IDN?":
            return b"Rohde&Schwarz,RTM3004,SIMULATED-AUTO,01.400"
        if command == "*OPT?":
            return b""
        if command == "*OPC?":
            if self.entered:
                self.entered.set()
                assert self.release.wait(5)
            return self.completion
        if command == "SYST:ERR?":
            value, self.error = self.error, '0,"No error"'
            return value.encode()
        if command.endswith((" MIN", " MAX")):
            query, bound = command.split(" ")
            return str(self.bounds[self.queries[query][1]][bound == "MAX"]).encode()
        if ":DATA:" in command:
            if command.endswith("HEAD?") and int(command[4]) in self.empty_channels:
                return b"0E+00,0E+00,0,0"
            span = self.values[1, "timebase"] * 12
            origin = self.values[1, "time_position"] - span / 2
            if command.endswith("HEAD?"):
                return f"{origin:.12e},{origin + span:.12e},64,1".encode()
            if command.endswith("XOR?"):
                return f"{origin:.12e}".encode()
            if command.endswith("XINC?"):
                return f"{span / 63:.12e}".encode()
        value = self.values[self.queries[command]]
        return str(int(value) if isinstance(value, bool) else value).encode()

    def query_binary(self, command, max_bytes):
        self.history.append(command)
        assert max_bytes == 4_000_000 * 4
        return ieee_block(np.zeros(64, dtype="<f4").tobytes())
