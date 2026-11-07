"""Finite recording through the public Agent facade, using the 53100A simulator."""

import threading
import time
from uuid import uuid4

import pytest

from lab_control.application.agents.policy import INTERNAL_CLIENT
from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceError
from lab_control.instruments.devices.microchip_53100a.tests.simulation import PhaseSimulation
from lab_control.storage.agent_ledger import ACTIVE


@pytest.fixture
def finite(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        runtime.sessions.factory = PhaseSimulation
        device = runtime.add(
            ConnectionConfig("mock://finite/phase", "microchip.53100a", "mock", usb_instance_id="TEST-USB")
        )
        runtime.connect(device).result(10)
        service = runtime.agent
        grant = service.policy.create_grant(
            INTERNAL_CLIENT,
            [device],
            ["view", "record", "acquire", "process", "results", "stop", "output_trigger"],
            channels=[1],
            operations=[],
            limits={},
        )
        source = next(
            s
            for s in service.get_capabilities(INTERNAL_CLIENT, grant, device)["data_sources"]
            if s["key"] == "phase_noise_capture"
        )
        assert "confirmed" not in source["request_defaults"]
        yield runtime, service, device, grant, dict(source["request_defaults"], channels=[1])


def settle(service, grant, task):
    deadline = time.monotonic() + 20
    while task["state"] in ACTIVE and time.monotonic() < deadline:
        time.sleep(0.02)
        task = service.get_task_status(INTERNAL_CLIENT, grant, task["id"])
    assert task["state"] not in ACTIVE
    return task


def test_finite_capture_publishes_complete_record_and_replay_is_idempotent(finite):
    runtime, service, device, grant, request = finite
    identifier = uuid4().hex
    task = service.start_recording(INTERNAL_CLIENT, grant, device, request, identifier)
    duplicate = service.start_recording(INTERNAL_CLIENT, grant, device, request, identifier)
    assert duplicate["id"] == task["id"]
    task = settle(service, grant, task)
    assert task["state"] == "completed" and task["result"] == {"complete": True, "frames": 62}
    result = service.start_experiment(
        INTERNAL_CLIENT,
        grant,
        device,
        uuid4().hex,
        "phase_noise.at_offset",
        parameters={"offset_hz": 10},
        recording_ids=[task["recording_id"]],
    )
    result = settle(service, grant, result)
    assert result["state"] == "completed"
    assert runtime.sessions.sessions[device].direct_recording.binary_done


def test_single_frame_entry_rejects_duration_and_record_requires_permission(finite):
    runtime, service, device, grant, request = finite
    task = service.acquire_once(INTERNAL_CLIENT, grant, device, request, uuid4().hex)
    assert task["state"] == "failed" and task["code"] == "invalid_parameter"
    assert runtime.sessions.sessions[device].direct_recording is None
    read_grant = service.policy.create_grant(
        INTERNAL_CLIENT, [device], ["view", "acquire"], channels=[1], operations=[], limits={}
    )
    with pytest.raises(DeviceError):
        service.start_recording(INTERNAL_CLIENT, read_grant, device, request, uuid4().hex)


def test_stop_waits_for_device_drain_and_does_not_publish_success(finite):
    runtime, service, device, grant, request = finite
    adapter = runtime.sessions.sessions[device].adapter
    original = adapter.run_acquisition
    entered, release = threading.Event(), threading.Event()

    def delayed(request, stop):
        for batch in original(request, stop):
            yield batch
            if not entered.is_set():
                entered.set()
                assert stop.wait(10)
                assert release.wait(10)

    adapter.run_acquisition = delayed
    task = service.start_recording(INTERNAL_CLIENT, grant, device, request, uuid4().hex)
    assert entered.wait(10)
    try:
        stopping = service.stop_task(INTERNAL_CLIENT, grant, task["id"], uuid4().hex)
        time.sleep(0.1)
        assert service.get_task_status(INTERNAL_CLIENT, grant, task["id"])["state"] == "stopping"
        assert runtime.sessions.sessions[device].pending
    finally:
        release.set()
    done = settle(service, grant, task)
    assert done["state"] == "cancelled"
    assert settle(service, grant, stopping)["state"] == "completed"


def test_truncated_finite_record_is_rejected(finite):
    runtime, service, device, grant, request = finite
    adapter = runtime.sessions.sessions[device].adapter
    original = adapter.run_acquisition

    def truncated(request, stop):
        frames = original(request, stop)
        try:
            yield next(frames)
        finally:
            frames.close()

    adapter.run_acquisition = truncated
    task = service.start_recording(INTERNAL_CLIENT, grant, device, request, uuid4().hex)
    task = settle(service, grant, task)
    assert task["state"] == "failed" and task["code"] == "incomplete_recording"
