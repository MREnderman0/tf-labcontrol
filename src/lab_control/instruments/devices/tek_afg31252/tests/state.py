"""Authored, sanitized AFG state for validation reads; writes remain strictly scripted."""

from lab_control.transport.base import MockTransport


def default_state():
    source = {
        "FUNC": "SIN",
        "FREQ": "1000",
        "FREQ:MODE": "CW",
        "FREQ:CONC": "0",
        "VOLT:CONC": "0",
        "PHAS:CONC": "0",
        "FSK:STAT": "0",
        "PWM:STAT": "0",
        "BURS:STAT": "0",
        "VOLT": "0.2",
        "VOLT:UNIT": "VPP",
        "VOLT:OFFS": "0",
        "VOLT:LIM:HIGH": "2.5",
        "VOLT:LIM:LOW": "-2.5",
        "COMB:FEED": '""',
        "AM:STAT": "0",
        "FM:STAT": "0",
        "PM:STAT": "0",
        "FM:DEV": "100",
        "AM:SOUR": "INT",
        "FM:SOUR": "INT",
        "PM:SOUR": "INT",
        "FREQ:STAR": "1000",
        "FREQ:STOP": "2000",
        "SWE:TIME": "1",
        "SWE:HTIM": "0",
        "SWE:RTIM": "0",
        "PULS:TRAN:LEAD": "2e-9",
        "PULS:TRAN:TRA": "2e-9",
        "PULS:HOLD": "DUTY",
        "PULS:DCYC": "50",
        "PULS:WIDT": "0.0005",
        "PULS:DEL": "0",
    }
    state = {f"SOUR{ch}:{path}?": value.encode() for ch in (1, 2) for path, value in source.items()}
    state.update(
        {f"OUTP{ch}{suffix}?": value for ch in (1, 2) for suffix, value in (("", b"0"), (":IMP", b"50"))}
    )
    return state


class StateTransport(MockTransport):
    def __init__(self, exchanges=(), overrides=None):
        super().__init__(list(exchanges))
        self.state = default_state()
        self.state.update(overrides or {})

    def write(self, payload):
        if self.exchanges and self.exchanges[0][0] == payload:
            super().write(payload)
            if payload in self.state and isinstance(self._reply, bytes):
                self.state[payload] = self._reply
        elif isinstance(payload, str) and payload in self.state:
            assert self.opened
            self.history.append(payload)
            self._reply = self.state[payload]
        else:
            super().write(payload)
