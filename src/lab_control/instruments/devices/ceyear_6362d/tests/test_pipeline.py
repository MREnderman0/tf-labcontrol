import time

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.controls import data_sources
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest
from lab_control.instruments.devices.ceyear_6362d.hardware.validate_control import install_metadata_sinks
from lab_control.instruments.devices.ceyear_6362d.tests.test_contract import Instrument, adapter


def wait_for(predicate):
    deadline = time.monotonic() + 4
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.005)


def connect(runtime, wire):
    config = ConnectionConfig("mock://6362d", "ceyear.6362d", "mock", expected_serial="TEST-6362D")
    runtime.sessions.factory = lambda selected: adapter(wire)
    key = runtime.add(config)
    runtime.connect(key, configure_acquisition=False).result(3)
    return key


def test_labels_distinguish_real_and_mock_without_changing_optical_card():
    real = ConnectionConfig("TCPIP0::example.invalid::10001::SOCKET", "ceyear.6362d")
    mock = ConnectionConfig("mock://6362d", "ceyear.6362d", "mock")
    assert data_sources(real)[0].label == "光谱曲线"
    assert data_sources(mock)[0].label == "光谱曲线（模拟）"


@pytest.mark.parametrize("bad", [b"", b"1,nan,3", b"#218short"])
def test_invalid_6362d_frame_never_displays_or_records(tmp_path, bad):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        wire = Instrument()
        wire.values[":TRACE:DATA:Y? TRA"] = bad
        key = connect(runtime, wire)
        report = {}
        install_metadata_sinks(runtime, report)
        with runtime.monitoring.lock:
            runtime.monitoring.start(
                key, (AcquisitionRequest("spectrum"),), mode="optical", record_interval=None
            )
            runtime.monitoring.set_recording(key, True)
        wait_for(lambda: not runtime.monitoring.tasks[key].running)
        runtime.monitoring.stop(key).result(3)
        assert runtime.monitoring.latest(key, "spectrum") is None
        assert not report.get("recorded_metadata")
        assert not list(runtime.repository.data_root.rglob("*.csv"))


def test_configuration_change_discard_and_slow_frames_record_without_backlog(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        wire = Instrument()
        key = connect(runtime, wire)
        calls = []

        def payload():
            calls.append(time.monotonic())
            time.sleep(0.06)
            if len(calls) == 1:
                wire.values[":SENSE:SENSE?"] = b"2"
            return b"-40,-20,-50"

        wire.values[":TRACE:DATA:Y? TRA"] = payload
        report = {}
        install_metadata_sinks(runtime, report)
        with runtime.monitoring.lock:
            runtime.monitoring.start(
                key,
                (AcquisitionRequest("spectrum"),),
                mode="optical",
                cadence="fastest",
                record_interval=None,
            )
            runtime.monitoring.set_recording(key, True)
        wait_for(lambda: runtime.monitoring.tasks[key].sequence >= 4)
        runtime.monitoring.stop(key).result(3)
        task = runtime.monitoring.tasks[key]
        assert not task.error
        audit = "".join(
            p.read_text(encoding="utf-8") for p in (runtime.repository.root / "logs").glob("*.jsonl")
        )
        assert '"event": "acquisition_discarded"' in audit
        assert len(report["recorded_metadata"]) == task.sequence
        assert len(calls) == task.sequence + 1
        assert all(b - a >= 0.055 for a, b in zip(calls, calls[1:]))
        assert not runtime.sessions.sessions[key].monitoring
        assert not list(runtime.repository.data_root.rglob("*.csv"))
