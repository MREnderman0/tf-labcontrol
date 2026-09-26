"""Deterministic SCPI peer; not a hardware qualification substitute."""

import numpy as np

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import DeviceError
from lab_control.transport.base import ieee_block

from ..adapter import DHO924Adapter
from ..profile import profile


class Peer:
    resource = "mock://dho924"

    def __init__(self):
        self.history = []
        self.fail = ""
        self.serial = "DHO-TEST"
        self.model = "DHO924"
        self.state = {
            ":TIM:MAIN:SCAL": 1e-6,
            ":TIM:MAIN:OFFS": 0,
            ":ACQ:MDEP": 10000,
            ":ACQ:SRAT": 1e9,
            ":TRIG:MODE": "EDGE",
            ":TRIG:EDGE:SOUR": "CHAN1",
            ":TRIG:EDGE:LEV": 0,
            ":TRIG:EDGE:SLOP": "POS",
            ":TRIG:SWE": "AUTO",
            ":TRIG:STAT": "AUTO",
            ":TRIG:HOLD": 1e-7,
            ":ACQ:TYPE": "NORM",
            ":ACQ:AVER": 2,
            ":TIM:MODE": "MAIN",
            ":TIM:ROLL": 0,
            ":TIM:XY:ENAB": 0,
            ":TIM:DEL:ENAB": 0,
            ":WAV:FORM": "BYTE",
            ":WAV:MODE": "NORM",
            ":WAV:SOUR": "CHAN1",
            ":WAV:STAR": 1,
            ":WAV:STOP": 1000,
            ":WAV:POIN": 1000,
        }
        for ch in range(1, 5):
            self.state.update(
                {
                    f":CHAN{ch}:{op}": value
                    for op, value in {
                        "DISP": 1,
                        "SCAL": 0.05 * ch,
                        "OFFS": 0,
                        "COUP": "DC",
                        "PROB": 1,
                        "UNIT": "VOLT",
                        "INV": 0,
                        "VERN": 0,
                    }.items()
                }
            )
        self.pre_calls = 0
        self.change_pre = False

    def open(self):
        pass

    def close(self):
        pass

    def write(self, payload):
        self.history.append(payload)
        if payload == self.fail:
            raise DeviceError("timeout", "timeout")
        if payload == "*OPC":
            return
        if payload in {":RUN", ":STOP", ":SING"}:
            self.state[":TRIG:STAT"] = {":RUN": "AUTO", ":STOP": "STOP", ":SING": "WAIT"}[payload]
            if payload == ":SING":
                self.state[":TRIG:SWE"] = "SING"
            return
        key, value = payload.split(" ", 1)
        if value in {"ON", "OFF"}:
            value = int(value == "ON")
        else:
            try:
                value = float(value)
            except ValueError:
                pass
        self.state[key] = value

    def query(self, payload):
        self.history.append(payload)
        if payload == self.fail:
            raise DeviceError("timeout", "timeout")
        if payload == "*IDN?":
            return f"RIGOL TECHNOLOGIES,{self.model},{self.serial},00.01.05\n".encode()
        if payload == "*OPC?":
            return b"1\n"
        if payload == ":SYST:ERR?":
            return b'0,"No error"\n'
        ch = int(self.state[":WAV:SOUR"][-1])
        points = int(self.state[":WAV:POIN"])
        if payload == ":WAV:PRE?":
            self.pre_calls += 1
            delta = 2 if self.change_pre and self.pre_calls % 2 == 0 else 1
            mode = 2 if self.state[":WAV:MODE"] == "RAW" else 0
            gain = self.state[f":CHAN{ch}:SCAL"] / 7500
            origin = self.state[":TIM:MAIN:OFFS"] - 5 * self.state[":TIM:MAIN:SCAL"]
            vertical = round(self.state[f":CHAN{ch}:OFFS"] / gain)
            return f"1,{mode},{points},1,{self.state[':TIM:MAIN:SCAL'] / 100},{origin},0,{gain * delta},{vertical},32768\n".encode()
        if payload == ":WAV:DATA?":
            return (
                ieee_block((np.arange(points, dtype=np.uint16) % 40 + 32768).astype("<u2").tobytes()) + b"\n"
            )
        return (str(self.state[payload[:-1]]) + "\n").encode()


def adapter():
    peer = Peer()
    obj = DHO924Adapter(peer, profile(), ConnectionConfig(peer.resource, "rigol.dho924", "mock"))
    obj.identify()
    return obj, peer
