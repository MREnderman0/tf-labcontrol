"""Exercise the real acceptance orchestration with an in-memory instrument."""

from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import DeviceError, DeviceIdentity, DeviceStatus

from ..hardware import control
from .test_protocol import COUPLING, wave


def completed(value):
    future = Future()
    future.set_result(value)
    return future


class FakeRuntime:
    def __init__(self, config, root, fail):
        self.config = config
        self.fail = fail
        self.commands = []
        self.values = {ch: dict(frequency=1000, amplitude=4, offset=0, phase=0) for ch in (1, 2)}
        self.identity = DeviceIdentity("Siglent Technologies", "SDG6032X-E", "TEST-SERIAL", "test-firmware")
        self.repository = SimpleNamespace(root=root)
        self.visa = SimpleNamespace(manager=SimpleNamespace(visalib="offline fake"))
        self.discovery = SimpleNamespace(scan=lambda **kw: completed({"items": [], "state": "complete"}))
        self.sessions = SimpleNamespace(
            sessions={}, submit=self.submit, disconnect=lambda id: self.sessions.sessions.pop(id)
        )
        self.closed = False

    def add(self, config):
        return "test-device"

    def connect(self, id, **kwargs):
        transport = SimpleNamespace(query=self.query, permitted_write=None)
        worker = SimpleNamespace(submit=lambda fn: completed(fn()))
        self.sessions.sessions[id] = SimpleNamespace(
            adapter=SimpleNamespace(transport=transport), worker=worker, state="connected"
        )
        return completed(self.identity)

    def query(self, query):
        if query == "*IDN?":
            return b"Siglent Technologies,SDG6032X-E,TEST-SERIAL,test-firmware"
        if query in {"COUP?", "MODE?", "ROSC?"}:
            return {"COUP?": COUPLING, "MODE?": b"MODE PHASE-LOCKED", "ROSC?": b"ROSC INT,10MOUT,OFF"}[query]
        ch = int(query[1])
        if query.endswith("BSWV?"):
            values = {
                key: int(value) if float(value).is_integer() else value
                for key, value in self.values[ch].items()
            }
            return wave(ch, **values)
        if query.endswith("OUTP?"):
            return f"C{ch}:OUTP OFF,LOAD,HZ".encode()
        return f"{query[:-1]} STATE,OFF".encode()

    def submit(self, id, operation, command=None):
        if operation == "status":
            return completed(DeviceStatus({}))
        self.commands.append((command.channel, command.operation, command.parameters["value"]))
        is_restore = len(self.commands) == 2
        is_failed_test = len(self.commands) == 1 and self.fail == "test-timeout"
        if not (is_restore and self.fail == "restore-mismatch"):
            self.values[command.channel][command.operation] = command.parameters["value"]
        if is_failed_test:
            self.sessions.sessions[id].state = "degraded"
            raise DeviceError("Injected read timeout after setting", "timeout")
        if is_restore and self.fail == "restore-mismatch":
            raise DeviceError("Injected restore failure", "timeout")
        return completed(SimpleNamespaceResult(command))

    def select_resource(self, *args, **kwargs):
        self.selected = True

    def close(self):
        self.closed = True


def SimpleNamespaceResult(command):
    from lab_control.domain.instruments import ApplyResult

    return ApplyResult("success", command.operation, command.parameters, command.parameters["value"], 1)


@pytest.mark.parametrize("failure", ["", "test-timeout", "restore-mismatch"])
def test_control_runner_restores_and_does_not_replay(failure, tmp_path, monkeypatch):
    config = ConnectionConfig(
        "TCPIP0::192.0.2.1::inst0::INSTR", "siglent.sdg6032xe", "visa", expected_serial="TEST-SERIAL"
    )
    fake = FakeRuntime(config, tmp_path, failure)
    monkeypatch.setattr(control, "ApplicationRuntime", lambda configs: fake)
    monkeypatch.setattr(control, "verify_runtime_paths", lambda *args: {"runtime": str(tmp_path)})
    monkeypatch.setattr(control, "hardware_lock_home", lambda: tmp_path)
    monkeypatch.setattr(control, "refresh_after_batch", lambda path: None)
    result = control.verify(config, tmp_path / "batch")
    assert fake.closed
    assert result["stages"]["physical_output"] == "not_tested"
    assert (tmp_path / "batch/initial_snapshot.json").exists()
    assert (tmp_path / "batch/results.json").exists()
    assert (tmp_path / "batch/summary.json").exists()
    if not failure:
        assert len(fake.commands) == 16 and len(result["reconnections"]) == 3
        assert result["stages"]["restoration"] == "passed"
    else:
        assert len(fake.commands) == 2  # One test and one restoration, never a second restoration.
        assert result["stages"]["parameter_write"] == "failed"
        assert result["stages"]["restoration"] == ("failed" if failure == "restore-mismatch" else "passed")


def test_write_guard_consumes_one_permission(tmp_path):
    writes = []
    inner = SimpleNamespace(resource="mock://guard", write=writes.append)
    guarded = control.TestTransport(inner, {"*IDN?"}, tmp_path / "io.jsonl")
    guarded.permitted_write = "C1:BSWV FRQ,1234"
    guarded.write("C1:BSWV FRQ,1234")
    for forbidden in ("C1:BSWV FRQ,1234", "C1:OUTP ON", "*RST"):
        with pytest.raises(DeviceError):
            guarded.write(forbidden)
    assert writes == ["C1:BSWV FRQ,1234"]
