"""Public session/monitor/recording contracts exercised with the actual model factory."""

import time

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError
from lab_control.instruments.registry import build_scpi_adapter

from ..profile import profile
from ..ranges import MEMORY_QUERIES, PATHS, cvar_query
from .samples import CHANNELS, GLOBALS, binary


class Scope:
    resource = "mock://9404m"

    def __init__(self):
        self.history = []
        self.values = {
            s.query.format(ch=ch): raw
            for op, raw in GLOBALS.items()
            for s in (profile().commands[op],)
            for ch in (1,)
        }
        self.values.update(
            {
                s.query.format(ch=ch): raw
                for op, raw in CHANNELS.items()
                for s in (profile().commands[op],)
                for ch in (1, 2, 3, 4)
            }
        )
        self.broken = False
        self.fail_write = ""
        self.closed = False

    def open(self):
        pass

    def close(self):
        self.closed = True

    def write(self, command):
        self.history.append(command)
        if command == self.fail_write:
            raise DeviceError("Timeout: uncertain write", "timeout")
        key, value = command.split(" ", 1)
        self.values[key + "?"] = value.encode()

    def query(self, command):
        self.history.append(command)
        if command == "*IDN?":
            return b"LECROY,WAVERUNNER9404M,TEST,9.2.0"
        if command == "*OPC?":
            return b"1"
        if command in MEMORY_QUERIES.values():
            return {"mode": b"SetMaximumMemory", "points": b"1000", "sample_rate": b"1e9"}[
                next(k for k, v in MEMORY_QUERIES.items() if v == command)
            ]
        for op in PATHS:
            spec = profile().commands[op].capability.parameters["value"]
            for ch in (1, 2, 3, 4):
                for member, value in (
                    ("GetMinValue", spec.minimum),
                    ("GetMaxValue", spec.maximum),
                    ("GetGrainValue", 0),
                ):
                    if command == cvar_query(op, ch, member):
                        return str(value).encode()
        if ":WF?" in command:
            raw = binary(int(command[1]))
            return raw[:-10] if self.broken else raw
        return self.values[command]


def wait_until(condition, seconds=10):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "Timed out waiting for public monitor state"
        time.sleep(0.01)


@pytest.fixture
def connected(tmp_path):
    config = ConnectionConfig("mock://9404m", "lecroy.9404m", "mock", expected_serial="TEST")
    with ApplicationRuntime((config,), root=tmp_path, hardware=False) as app:
        wire = Scope()
        app.sessions.factory = lambda selected: build_scpi_adapter(selected, wire)
        key = app.add(config)
        app.connect(key, configure_acquisition=False).result(5)
        yield app, key, wire


def test_readonly_connection_does_not_prepare_and_explicit_connection_prepares(connected):
    app, key, wire = connected
    assert wire.history[0] == "*IDN?" and all("?" in q for q in wire.history)
    app.sessions.disconnect(key)
    wire.closed = False
    wire.values["CFMT?"] = b"DEF9,BYTE,BIN"
    app.connect(key).result(5)
    assert wire.history.count("CFMT DEF9,WORD,BIN") == 1
    assert app.sessions.submit(key, "acquire", AcquisitionRequest("waveform")).result(5).validation == "full"
    assert wire.history.count("CFMT DEF9,WORD,BIN") == 1


def test_monitor_edits_record_lock_binary_csv_and_release(connected):
    app, key, wire = connected
    request = AcquisitionRequest("waveform", points=4)
    app.monitoring.start(key, (request,), interval=0.05, record_interval=0.05)
    wait_until(lambda: app.monitoring.latest(key, "waveform", channel=1) is not None)
    assert (
        app.sessions.submit(key, "apply", DeviceCommand("timebase", {"value": 0.002})).result(5).outcome
        == "success"
    )
    app.monitoring.set_recording(key, True)
    count = len(wire.history)
    with pytest.raises(DeviceError, match="锁定"):
        app.sessions.submit(key, "apply", DeviceCommand("timebase", {"value": 0.003}))
    assert not any(command == "TDIV 0.003" for command in wire.history[count:])
    recorder = app.monitoring.tasks[key].recorder
    wait_until(lambda: recorder.snapshot()["committed"] >= 2)
    app.monitoring.stop(key).result(15)
    recorder.finished.result(15)
    assert recorder.snapshot()["binary_done"] and not recorder.snapshot()["csv_error"]
    assert tuple(recorder.folder.rglob("*.csv"))
    assert (
        app.sessions.submit(key, "apply", DeviceCommand("timebase", {"value": 0.001})).result(5).outcome
        == "success"
    )


def test_invalid_frame_never_published_or_saved(connected):
    app, key, wire = connected
    wire.broken = True
    app.monitoring.start(key, (AcquisitionRequest("waveform"),), interval=0.05, once=True)
    wait_until(lambda: not app.monitoring.snapshots()[key]["running"])
    assert app.monitoring.latest(key, "waveform", channel=1) is None


def test_oversized_capture_stops_task_but_retains_editable_connection(connected):
    app, key, wire = connected
    adapter = app.sessions.sessions[key].adapter
    adapter.capability_details["memory"] = {"mode": "FixedSampleRate", "points": 64_000_000}
    app.monitoring.start(key, (AcquisitionRequest("waveform"),), interval=0.5)
    wait_until(lambda: not app.monitoring.snapshots()[key]["running"])
    assert "采集点数" in app.monitoring.snapshots()[key]["error"]
    assert app.sessions.sessions[key].state == "connected"
    assert not any("WF?" in q for q in wire.history)
    assert not tuple(app.repository.root.rglob("samples.csv"))
    assert app.monitoring.snapshots()[key]["error"]


def test_uncertain_write_is_sent_once_and_session_degrades(connected):
    app, key, wire = connected
    wire.fail_write = "TDIV 0.002"
    result = app.sessions.submit(key, "apply", DeviceCommand("timebase", {"value": 0.002})).result(5)
    assert result.outcome == "uncertain" and wire.history.count(wire.fail_write) == 1
    with pytest.raises(DeviceError):
        app.sessions.submit(key, "apply", DeviceCommand("timebase", {"value": 0.002}))
    assert wire.history.count(wire.fail_write) == 1


def test_restoration_handles_impedance_before_large_scale_and_exact_transfer(connected, tmp_path):
    from ..hardware.validate_control import Verification

    app, key, wire = connected
    runner = Verification(app, key, tmp_path, {"operations": []})
    desired = runner.snapshot()
    desired.update({"1.scale": 2.0, "1.coupling": "D1M", "1.transfer_format": "DEF9,BYTE,BIN"})
    wire.values.update({"C1:VDIV?": b".2 V", "C1:CPL?": b"D50"})
    runner.restore_parameters(desired)
    assert runner.snapshot() == desired
    assert wire.history.index("C1:CPL D1M") < wire.history.index("C1:VDIV 2")


def test_failed_restoration_is_reported_without_replaying_write(connected, tmp_path):
    from ..hardware.validate_control import Verification

    app, key, wire = connected
    runner = Verification(app, key, tmp_path, {"operations": []})
    desired = runner.snapshot()
    wire.values["TDIV?"] = b".002 S"
    wire.fail_write = "TDIV 0.001"
    with pytest.raises(DeviceError):
        runner.restore_parameters(desired)
    assert wire.history.count(wire.fail_write) == 1


def test_hardware_capture_entry_uses_public_save_and_recording_interfaces(connected, tmp_path):
    from ..hardware.validate_control import Verification

    app, key, _ = connected
    report = {"operations": [], "stages": {}}
    runner = Verification(app, key, tmp_path, report)
    before = runner.snapshot()
    try:
        runner.capture(1.2)
        assert report["stages"] == {
            "waveform": "passed",
            "single_save": "passed",
            "monitoring_recording": "passed",
        }
        assert report["recording"]["converted"] >= 1
    finally:
        runner.restore_parameters(before)
