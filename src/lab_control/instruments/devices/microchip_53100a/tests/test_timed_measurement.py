import socket
import threading
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceError, DeviceIdentity
from lab_control.instruments.devices.microchip_53100a import measurement
from lab_control.instruments.devices.microchip_53100a.adapter import Microchip53100AAdapter
from lab_control.instruments.devices.microchip_53100a.profile import profile
from lab_control.transport.base import MockTransport

REQUEST = AcquisitionRequest("timed_phase", duration=10, sample_rate=10, initiate=True, confirmed=True)
SPECTRUM = "Time Constant: Infinite\nFrequency (Hz)\tPower Spectral Density (dBc/Hz)\n1 -90\n10 -110\nNoise Floor\nFrequency (Hz)\tPSD\n1 -120\n10 -130"


@pytest.fixture
def instrument(monkeypatch):
    adapter = Microchip53100AAdapter(
        MockTransport([]),
        profile(),
        ConnectionConfig("mock://finite", "microchip.53100a", "mock", usb_instance_id="TEST-USB"),
    )
    adapter.identity = DeviceIdentity("Microchip", "53100A", "TEST123")
    adapter.server = SimpleNamespace(data_port=1234)
    state = {
        "rate": 100,
        "running": False,
        "serial": "TEST123",
        "stop_fail": False,
        "inputfreq": "Auto",
        "referencefreq": "Auto",
        "frequency_fail": False,
    }
    history = []

    def query(command):
        history.append(command)
        if command == "show state":
            return "Collecting" if state["running"] else "Ready"
        if command == "show phaserate":
            return f"Network port phase output rate: {state['rate']} samples/sec"
        if command in ("show inputfreq", "show referencefreq"):
            return state[command.split()[-1]]
        if command.startswith(("set inputfreq ", "set referencefreq ")):
            if state["frequency_fail"]:
                raise DeviceError("Uncertain frequency write", "timeout")
            state[command.split()[1]] = "Auto"
            return ""
        if command.startswith("set phaserate "):
            state["rate"] = int(command.split()[-1])
            return ""
        if command == "start":
            state["running"] = True
            return ""
        if command == "stop":
            if state["stop_fail"]:
                raise DeviceError("Simulated stop timeout", "timeout")
            state["running"] = False
            return ""
        if command == "show serial":
            return state["serial"]
        if command == "show spectrum":
            return SPECTRUM
        pytest.fail("Unexpected command: " + command)

    adapter._query = query

    class Stream:
        closed = False
        count = 0
        fail_at = None

        def __init__(self, *_):
            pass

        def read(self, stop, timeout):
            if stop.is_set():
                return None
            if self.count == self.fail_at:
                raise DeviceError("Injected data gap", "data_gap")
            self.count += 1
            return self.count / state["rate"], self.count**2 * 1e-12

        def close(self):
            Stream.closed = True

    monkeypatch.setattr(measurement, "PhaseStream", Stream)
    return adapter, state, history, Stream


def test_complete_run_saves_exact_span_both_statistics_and_stops_once(instrument):
    adapter, state, history, stream = instrument
    frames = list(adapter.run_acquisition(REQUEST, threading.Event()))
    phase = [m for frame in frames for m in frame.measurements if m.quantity == "phase_difference"]
    times = np.concatenate([m.axis for m in phase])
    assert len(times) == 101 and times[-1] - times[0] == pytest.approx(10)
    assert {m.quantity for m in frames[-1].measurements} == {"phase_noise", "noise_floor", "adev", "mdev"}
    assert frames[-1].configuration["complete"] and frames[-1].configuration["stop_state"] == "Ready"
    assert history.count("start") == history.count("stop") == history.count("set phaserate 10") == 1
    assert not state["running"] and stream.closed
    spectra = [m for frame in frames[:-1] for m in frame.measurements if m.quantity == "phase_noise"]
    assert len(spectra) == history.count("show spectrum")
    assert all(m.metadata["queried_at"] for m in spectra)
    assert frames[-1].measurements[0].metadata == spectra[-1].metadata


def test_minute_capture_sets_both_frequencies_auto_before_start(instrument):
    adapter, state, history, _ = instrument
    state.update(inputfreq="10 MHz", referencefreq="20 MHz")
    frames = list(adapter.run_acquisition(replace(REQUEST, sample_rate=1000, duration=60), threading.Event()))
    phase = [m for frame in frames for m in frame.measurements if m.quantity == "phase_difference"]
    times = np.concatenate([m.axis for m in phase])
    assert times.size == 60001 and times[-1] - times[0] == pytest.approx(60)
    for name in ("inputfreq", "referencefreq"):
        assert history.count(f"set {name} auto") == 1
        assert history.index(f"set {name} auto") < history.index("start")
        assert state[name] == "Auto"
    assert state["rate"] == 1000 and not state["running"]
    assert frames[-1].configuration["original_settings"]["reference_frequency"]["value"] == 20e6
    assert all(value["automatic"] for value in frames[-1].configuration["settings"].values())
    assert {m.quantity for m in frames[-1].measurements} >= {"adev", "mdev"}


def test_uncertain_auto_write_prevents_acquisition_and_replay(instrument):
    adapter, state, history, _ = instrument
    state.update(inputfreq="10 MHz", referencefreq="20 MHz", frequency_fail=True)
    with pytest.raises(DeviceError) as failure:
        list(adapter.run_acquisition(REQUEST, threading.Event()))
    assert failure.value.code == "configuration_uncertain"
    assert history.count("set inputfreq auto") == 1
    assert "set referencefreq auto" not in history and "start" not in history


def test_stop_retains_partial_valid_data_and_labels_partial(instrument):
    adapter, _, history, _ = instrument
    stop = threading.Event()
    run = adapter.run_acquisition(REQUEST, stop)
    next(run)
    stop.set()
    final = list(run)[-1]
    assert final.configuration["samples"] == 10
    assert final.configuration["final"] and not final.configuration["complete"]
    assert history.count("stop") == 1


def test_generator_close_for_buffer_pressure_stops_instrument(instrument):
    adapter, state, history, _ = instrument
    run = adapter.run_acquisition(REQUEST, threading.Event())
    next(run)
    run.close()
    assert history.count("stop") == 1 and not state["running"]


@pytest.mark.parametrize("failure", ["gap", "identity", "configuration", "cleanup"])
def test_failure_never_publishes_invalid_or_complete_result(instrument, failure):
    adapter, state, history, stream = instrument
    if failure == "identity":
        state["serial"] = "FOREIGN"
    if failure == "gap":
        stream.fail_at = 3
    if failure == "cleanup":
        state["stop_fail"] = True
    run = adapter.run_acquisition(REQUEST, threading.Event())
    frames = []
    if failure == "configuration":
        frames.append(next(run))
        state["rate"] = 100
    with pytest.raises(DeviceError):
        frames.extend(run)
    assert all(not f.configuration["complete"] for f in frames)
    assert history.count("start") == 1
    assert history.count("stop") == (0 if failure == "gap" else 1)


def test_gap_resumes_active_run_without_start_or_settings_replay(instrument):
    adapter, state, history, stream = instrument
    stream.fail_at = 13
    run = adapter.run_acquisition(REQUEST, threading.Event())
    first = next(run)
    with pytest.raises(DeviceError, match="gap"):
        next(run)
    assert state["running"] and "stop" not in history
    resume_state = dict(adapter.recorded_run_state)
    stream.fail_at = None
    frames = list(adapter.resume_acquisition(REQUEST, threading.Event(), resume_state, 1.3))
    assert history.count("start") == history.count("set phaserate 10") == history.count("stop") == 1
    assert frames[-1].configuration["samples"] == 91
    assert frames[-1].configuration["acquisition_epoch"] == first.configuration["acquisition_epoch"] + 1
    assert frames[-1].configuration["has_data_gaps"]
    assert frames[-1].configuration["stability_scope"] == "current_contiguous_epoch"


def test_resume_refuses_changed_rate_without_restoring_settings(instrument):
    adapter, state, history, stream = instrument
    stream.fail_at = 3
    with pytest.raises(DeviceError):
        list(adapter.run_acquisition(REQUEST, threading.Event()))
    state["rate"] = 100
    before = list(history)
    with pytest.raises(DeviceError, match="参数"):
        list(adapter.resume_acquisition(REQUEST, threading.Event(), adapter.recorded_run_state, 1))
    assert not any(command.startswith("set ") or command == "start" for command in history[len(before) :])


def test_public_recording_runner_resumes_finite_phase_stream(instrument, tmp_path):
    from pathlib import Path

    from lab_control.application.runtime import ApplicationRuntime
    from lab_control.storage.recording import read_frames

    adapter, state, history, stream = instrument
    identity = adapter.identity
    adapter.identify = lambda: identity
    adapter.close = lambda: None
    stream.fail_at = 13

    def reopen():
        stream.fail_at = None

    adapter.reopen_acquisition = reopen
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        runtime.sessions.factory = lambda _c: adapter
        device = runtime.add(adapter.config)
        runtime.connect(device, configure_acquisition=False).result(3)
        paths = runtime.sessions.start_acquisition(device, REQUEST).result(10)
        frames = [f for p in paths for f in read_frames(Path(p))]
        assert sum("_acquisition_gap" in f["configuration"] for f in frames) == 1
        assert frames[-1]["configuration"]["complete"]
        assert frames[-1]["configuration"]["acquisition_epoch"] == 1
        assert history.count("start") == history.count("stop") == 1
        assert not state["running"]


@pytest.mark.parametrize("stop_fails", [False, True])
def test_missing_signal_ends_run_without_reconnect_and_verifies_stop(
    instrument, tmp_path, monkeypatch, stop_fails
):
    from lab_control.application.runtime import ApplicationRuntime

    adapter, state, history, stream = instrument
    identity = adapter.identity
    adapter.identify = lambda: identity
    adapter.close = lambda: None
    state["stop_fail"] = stop_fails
    original_read = stream.read

    def missing_signal(*args):
        raise DeviceError("未检测到 DUT 或参考输入信号", "signal_missing")

    def unexpected_reconnect():
        pytest.fail("Missing signal must not reopen the connection")

    monkeypatch.setattr(stream, "read", missing_signal)
    adapter.reopen_acquisition = unexpected_reconnect
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        runtime.sessions.factory = lambda _c: adapter
        device = runtime.add(adapter.config)
        runtime.connect(device, configure_acquisition=False).result(3)
        with pytest.raises(DeviceError) as failure:
            runtime.sessions.start_acquisition(device, REQUEST).result(5)
        assert failure.value.code == ("cleanup_failed" if stop_fails else "signal_missing")
        session = runtime.sessions.sessions[device]
        assert session.state == ("degraded" if stop_fails else "connected")
        assert not session.recovery and not session.active_task
        assert session.direct_recording.binary_done and session.direct_recording.committed == 0
        assert history.count("start") == history.count("stop") == 1
        assert stream.closed and state["running"] == stop_fails
        if stop_fails:
            assert "unknown" in str(failure.value)
        else:
            # A later explicit click can succeed on the same session after inputs are connected.
            monkeypatch.setattr(stream, "read", original_read)
            paths = runtime.sessions.start_acquisition(device, REQUEST).result(5)
            assert paths and history.count("start") == history.count("stop") == 2


@pytest.mark.parametrize(
    "changes",
    [{"sample_rate": 2}, {"duration": 9}, {"duration": 86400, "sample_rate": 1000}, {"confirmed": False}],
)
def test_invalid_request_does_not_send_io(instrument, changes):
    adapter, _, history, _ = instrument
    with pytest.raises(DeviceError):
        list(adapter.run_acquisition(replace(REQUEST, **changes), threading.Event()))
    assert not history


def test_real_stream_framing_and_timestamp_validation(monkeypatch):
    a, b = socket.socketpair()
    monkeypatch.setattr(measurement.socket, "create_connection", lambda *_: a)
    stream = measurement.PhaseStream(1234, 1000, 1)
    try:
        b.sendall(b"0.001000 0.0000000000010000\r\n0.002000 2e-12\r\n0.004000 3e-12\r\n")
        assert stream.read(threading.Event(), 1) == (0.001, 1e-12)
        assert stream.read(threading.Event(), 1) == (0.002, 2e-12)
        with pytest.raises(DeviceError, match="gap"):
            stream.read(threading.Event(), 1)
    finally:
        stream.close()
        b.close()


@pytest.mark.parametrize(
    "raw", [b"warning: input overload\n", b"0.1 nan\n", b"0.1 1 2\n", b"0.1,1\n", b"\xff\n"]
)
def test_stream_rejects_notifications_invalid_format_and_nan(monkeypatch, raw):
    a, b = socket.socketpair()
    monkeypatch.setattr(measurement.socket, "create_connection", lambda *_: a)
    stream = measurement.PhaseStream(1234, 10, 1)
    try:
        b.sendall(raw)
        with pytest.raises(DeviceError) as failure:
            stream.read(threading.Event(), 1)
        assert failure.value.code == "parse_error"
    finally:
        stream.close()
        b.close()


@pytest.mark.parametrize(
    "raw",
    [
        b"      No signal found in one or more channels\r\n",
        b"No signals detected at front-panel input jacks.  Check source connections\r\n",
    ],
)
def test_known_missing_signal_notification_is_not_a_transport_failure(monkeypatch, raw):
    from lab_control.application.acquisition_recovery import recoverable

    a, b = socket.socketpair()
    monkeypatch.setattr(measurement.socket, "create_connection", lambda *_: a)
    stream = measurement.PhaseStream(1234, 1000, 1)
    try:
        b.sendall(raw)
        with pytest.raises(DeviceError, match="未检测到 DUT 或参考输入信号") as failure:
            stream.read(threading.Event(), 1)
        assert failure.value.code == "signal_missing" and not recoverable(failure.value)
    finally:
        stream.close()
        b.close()


def test_spectrum_headers_empty_floor_and_invalid_rows():
    assert len(measurement.spectrum(SPECTRUM)) == 2
    assert measurement.spectrum("") == ()
    assert len(measurement.spectrum("1 -90\nNoise Floor\nFrequency (Hz) PSD")) == 1
    for raw in ("1 nan", "2 -90\n1 -100", "Data unavailable", "1 -90\ntruncated"):
        with pytest.raises(DeviceError):
            measurement.spectrum(raw)


def test_sample_rate_mismatch_stops_before_start_and_never_replays(instrument):
    adapter, _, history, _ = instrument
    query = adapter._query

    def mismatch(command):
        if command.startswith("set phaserate"):
            history.append(command)
            return ""
        return query(command)

    adapter._query = mismatch
    with pytest.raises(DeviceError, match="no replay"):
        list(adapter.run_acquisition(REQUEST, threading.Event()))
    assert history.count("set phaserate 10") == 1
    assert "start" not in history and "stop" not in history


def test_start_timeout_is_not_retried_and_never_publishes_a_result(instrument):
    adapter, _, history, _ = instrument
    query = adapter._query

    def fail(command):
        if command == "start":
            history.append(command)
            raise DeviceError("Start response timed out", "timeout")
        return query(command)

    adapter._query = fail
    with pytest.raises(DeviceError, match="timed out"):
        list(adapter.run_acquisition(REQUEST, threading.Event()))
    assert history.count("start") == history.count("stop") == 1
