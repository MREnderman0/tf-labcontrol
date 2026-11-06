"""Test-only model capabilities; no production algorithm or physical I/O."""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import replace
from uuid import uuid4

import numpy as np
import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest, DeviceError
from lab_control.domain.metrics import (
    CapturePlan,
    MetricDefinition,
    MetricParameters,
    MetricProfile,
    MetricValue,
)
from lab_control.instruments.registry import DEVICE_DEFINITIONS
from lab_control.storage.agent_ledger import ACTIVE


class Parameters(MetricParameters):
    scale: float = 1.0


class CaptureParameters(MetricParameters):
    frames: int = 1


def settle(service, client, grant, task, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = service.get_task_status(client, grant, task["id"])
        if value["state"] not in ACTIVE:
            return value
        time.sleep(0.02)
    raise AssertionError(value)


@pytest.fixture
def metrics(tmp_path, monkeypatch):
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    device = runtime.add(ConnectionConfig("mock://metrics/scope", "rs.rtm3004", "mock"))
    runtime.connect(device).result()
    service = runtime.agent
    client, token = service.policy.create_client("metrics")
    grant = service.policy.create_grant(
        client,
        [device],
        ["view", "connect", "acquire", "record", "stop", "results", "process"],
        channels=[1, 2],
        operations=[],
        limits={},
    )
    calls = []

    def process(source, parameters):
        calls.append(source.recording_id)
        values = []
        for frame in source.frames():
            source.check_cancel()
            values.extend(np.asarray(m["values"]).mean() for m in frame["measurements"])
        source.progress(len(values), len(values))
        return {"mean": MetricValue(float(np.mean(values)) * parameters.scale)}

    def capture(parameters):
        request = AcquisitionRequest("waveform", points=32)
        return (
            CapturePlan(request)
            if parameters.frames == 1
            else CapturePlan(request, "count", count=parameters.frames, interval=0.05)
        )

    profile = MetricProfile(
        "test.mean",
        "1",
        "测试专用指标",
        Parameters,
        (MetricDefinition("mean", "V", "Test mean"),),
        process,
        CaptureParameters,
        capture,
    )
    definition = DEVICE_DEFINITIONS["rs.rtm3004"]
    monkeypatch.setitem(DEVICE_DEFINITIONS, "rs.rtm3004", replace(definition, metric_profiles=(profile,)))
    yield runtime, service, client, grant, device, profile, calls, token
    runtime.close()


def capture_record(env, channels=(1,)):
    runtime, service, client, grant, device, *_ = env
    task = service.acquire_once(
        client, grant, device, {"kind": "waveform", "points": 32, "channels": list(channels)}, uuid4().hex
    )
    done = settle(service, client, grant, task)
    assert done["state"] == "completed", done
    return done["recording_id"]


def process_record(env, recording_id, **kwargs):
    _, service, client, grant, device, *_ = env
    return service.start_processing(
        client, grant, device, kwargs.pop("request_id", uuid4().hex), recording_id, "test.mean", **kwargs
    )


def test_production_defaults_are_disabled():
    assert all(not definition.metric_profiles for definition in DEVICE_DEFINITIONS.values())


def test_steps_cache_force_versions_and_no_duplicate_arrays(metrics, monkeypatch):
    runtime, service, client, grant, device, profile, calls, _ = metrics
    record_id = capture_record(metrics)
    record = service.metrics.store.resolve(record_id)
    raw = record.device.folder / "_raw"
    saved = json.loads((raw / "snapshot.json").read_text(encoding="utf-8"))
    assert saved["snapshot_format_version"] == 2
    assert all("values" not in m and "axis" not in m for m in saved["measurements"])
    assert list(record.device.folder.glob("*.csv"))
    first = process_record(metrics, record_id, request_id="first")
    done = settle(service, client, grant, first)
    assert done["state"] == "completed", done
    result = service.get_processing_result(client, grant, done["result_id"])
    assert result["metrics"]["mean"]["quality"] == "simulated"
    assert result["metrics"]["mean"]["unit"] == "V"
    assert len(calls) == 1
    assert process_record(metrics, record_id, request_id="first")["id"] == first["id"]
    second = settle(service, client, grant, process_record(metrics, record_id))
    assert second["result"]["reused"] and second["result_id"] == done["result_id"]
    assert len(calls) == 1
    forced = settle(service, client, grant, process_record(metrics, record_id, force=True))
    assert forced["result_id"] != done["result_id"] and len(calls) == 2
    definition = DEVICE_DEFINITIONS["rs.rtm3004"]
    monkeypatch.setitem(
        DEVICE_DEFINITIONS,
        "rs.rtm3004",
        replace(definition, metric_profiles=(replace(profile, version="2"),)),
    )
    new = settle(service, client, grant, process_record(metrics, record_id))
    assert new["result_id"] not in {done["result_id"], forced["result_id"]} and len(calls) == 3
    monkeypatch.setitem(DEVICE_DEFINITIONS, "rs.rtm3004", replace(definition, metric_profiles=()))
    assert service.get_processing_result(client, grant, done["result_id"]) == result
    assert not list(record.device.folder.rglob("*.png"))
    assert not (record.device.folder / ".processing-cache").exists()
    assert len(service.list_processing_results(client, grant, recording_id=record_id)) == 3
    assert service.get_processing_capabilities(client, grant, device)["supported"] is False


@pytest.mark.parametrize("frames", [1, 3])
def test_automatic_and_offline_discovery(metrics, frames):
    runtime, service, client, grant, device, _, calls, _ = metrics
    task = service.start_experiment(
        client, grant, device, uuid4().hex, "test.mean", capture_parameters={"frames": frames}
    )
    done = settle(service, client, grant, task)
    assert done["state"] == "completed", done
    assert len(calls) == 1
    records = service.list_recordings(client, grant)
    assert records[0]["recording_id"] == done["recording_id"]
    assert records[0]["frames"] == frames and records[0]["complete"]
    runtime.sessions.disconnect(device)
    assert service.get_processing_capabilities(client, grant, device)["supported"]
    assert service.get_processing_result(client, grant, done["result_id"])["complete"]


def test_channels_grant_and_parameter_rejection_before_capture(metrics):
    runtime, service, client, grant, device, _, calls, _ = metrics
    record_id = capture_record(metrics, (1, 2))
    limited = service.policy.create_grant(
        client, [device], ["view", "results", "process"], channels=[1], operations=[], limits={}
    )
    assert service.list_recordings(client, limited) == []
    denied = service.start_processing(client, limited, device, "denied", record_id, "test.mean")
    assert settle(service, client, limited, denied)["code"] == "permission_denied"
    no_process = service.policy.create_grant(
        client, [device], ["view", "results"], channels=[1, 2], operations=[], limits={}
    )
    with pytest.raises(DeviceError):
        service.start_processing(client, no_process, device, "no-process", record_id, "test.mean")
    with pytest.raises(ValueError):
        service.start_experiment(
            client, grant, device, "invalid", "test.mean", capture_parameters={"unknown": 1}
        )
    with pytest.raises(DeviceError, match="未声明"):
        service.start_experiment(client, grant, device, "unsupported", "missing")
    assert not calls
    assert not service.metrics.pending


def test_automatic_capture_respects_existing_acquisition(metrics):
    runtime, service, client, grant, device, _, calls, _ = metrics
    existing = runtime.sessions.start_acquisition(
        device, AcquisitionRequest("waveform", points=32), count=100, interval=0.05
    )
    recorder = runtime.sessions.sessions[device].direct_recording
    try:
        task = service.start_experiment(client, grant, device, "device-busy", "test.mean")
        done = settle(service, client, grant, task)
        assert done["state"] == "failed" and done["code"] == "task_busy"
        assert not calls and runtime.sessions.sessions[device].direct_recording is recorder
        assert not runtime.sessions.sessions[device].stop.is_set()
    finally:
        runtime.sessions.stop_acquisition(device)
        existing.result(10)


def test_request_conflict_and_source_change(metrics):
    _, service, client, grant, _, _, calls, _ = metrics
    record_id = capture_record(metrics)
    done = settle(service, client, grant, process_record(metrics, record_id, request_id="same"))
    with pytest.raises(DeviceError, match="request_id"):
        process_record(metrics, record_id, request_id="same", parameters={"scale": 2.0})
    changed = settle(service, client, grant, process_record(metrics, record_id, parameters={"scale": 2.0}))
    assert changed["result_id"] != done["result_id"] and len(calls) == 2


def test_cancel_join_queue_and_file_reservation(metrics, monkeypatch):
    runtime, service, client, grant, _, profile, _, _ = metrics
    record_id = capture_record(metrics)
    entered = threading.Event()
    release = threading.Event()

    def slow(source, parameters):
        entered.set()
        while not release.wait(0.02):
            source.check_cancel()
        return {"mean": MetricValue(1.0)}

    definition = DEVICE_DEFINITIONS["rs.rtm3004"]
    monkeypatch.setitem(
        DEVICE_DEFINITIONS,
        "rs.rtm3004",
        replace(definition, metric_profiles=(replace(profile, process=slow),)),
    )
    task = process_record(metrics, record_id)
    assert entered.wait(3)
    same = process_record(metrics, record_id, request_id="join")
    assert same["id"] == task["id"]
    assert process_record(metrics, record_id, request_id="join")["id"] == task["id"]
    entry = runtime.processing.catalog.scan()[0]
    with pytest.raises(DeviceError, match="已有处理"):
        runtime.processing.rename(entry, "cannot-move")
    monkeypatch.setattr(service.metrics, "capacity", 1)
    with pytest.raises(DeviceError, match="队列"):
        process_record(metrics, record_id, force=True)
    stop = service.stop_task(client, grant, task["id"], "cancel")
    assert settle(service, client, grant, stop)["state"] == "completed"
    assert settle(service, client, grant, task)["state"] == "cancelled"
    assert service.list_processing_results(client, grant) == []
    release.set()
    assert not runtime.repository.record_access.held


def test_revocation_cancels_processing_and_does_not_publish(metrics, monkeypatch):
    _, service, client, grant, _, profile, _, _ = metrics
    record_id = capture_record(metrics)
    entered = threading.Event()

    def slow(source, parameters):
        entered.set()
        while True:
            time.sleep(0.02)
            source.check_cancel()

    monkeypatch.setitem(
        DEVICE_DEFINITIONS,
        "rs.rtm3004",
        replace(DEVICE_DEFINITIONS["rs.rtm3004"], metric_profiles=(replace(profile, process=slow),)),
    )
    task = process_record(metrics, record_id)
    assert entered.wait(3)
    service.policy.revoke_grant(grant)
    deadline = time.monotonic() + 4
    while service.ledger.get(task["id"])["state"] in ACTIVE and time.monotonic() < deadline:
        time.sleep(0.02)
    assert service.ledger.get(task["id"])["state"] in {"failed", "cancelled"}
    assert not service.metrics.store.results(service.metrics.store.resolve(record_id))


def test_move_duplicate_ids_and_result_tampering(metrics):
    runtime, service, client, grant, _, _, calls, _ = metrics
    record_id = capture_record(metrics)
    done = settle(service, client, grant, process_record(metrics, record_id))
    entry = runtime.processing.catalog.scan()[0]
    moved = runtime.processing.rename(entry, "renamed-record").result(5)
    record = service.metrics.store.resolve(record_id)
    assert str(record.device.folder) == moved["destination"]
    assert service.get_processing_result(client, grant, done["result_id"])["id"] == done["result_id"]
    assert settle(service, client, grant, process_record(metrics, record_id))["result"]["reused"]
    assert len(calls) == 1
    copy = record.device.folder.with_name("copy")
    shutil.copytree(record.device.folder, copy)
    with pytest.raises(DeviceError, match="多个目录"):
        service.metrics.store.resolve(record_id)


@pytest.mark.parametrize("failure", ["algorithm", "nan", "source", "index", "journal"])
def test_failures_never_publish_success(metrics, monkeypatch, failure):
    _, service, client, grant, _, profile, _, _ = metrics
    record_id = capture_record(metrics)
    record = service.metrics.store.resolve(record_id)

    def bad(source, parameters):
        if failure == "algorithm":
            raise ValueError("test failure")
        if failure == "source":
            path = record.device.folder / "_raw" / "snapshot.json"
            path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
        return {"mean": MetricValue(float("nan") if failure == "nan" else 1.0)}

    monkeypatch.setitem(
        DEVICE_DEFINITIONS,
        "rs.rtm3004",
        replace(DEVICE_DEFINITIONS["rs.rtm3004"], metric_profiles=(replace(profile, process=bad),)),
    )
    if failure == "index":
        monkeypatch.setattr(
            service.metrics.store, "index", lambda *a: (_ for _ in ()).throw(OSError("index full"))
        )
    if failure == "journal":
        original = service.ledger.update

        def fail_completion(task):
            if task.get("phase") == "completed":
                raise OSError("journal full")
            original(task)

        monkeypatch.setattr(service.ledger, "update", fail_completion)
    done = settle(service, client, grant, process_record(metrics, record_id))
    assert done["state"] == "failed", done
    assert not done.get("result_id")
    assert not list((record.device.folder / "指标结果").glob("*.json"))


@pytest.mark.parametrize("damage", ["not_object", "missing", "nan", "channels"])
def test_malformed_imported_result_is_unavailable(metrics, damage):
    runtime, service, client, grant, _, _, _, _ = metrics
    record_id = capture_record(metrics)
    done = settle(service, client, grant, process_record(metrics, record_id))
    record = service.metrics.store.resolve(record_id)
    path = record.device.folder / "指标结果" / (done["result_id"] + ".json")
    result = json.loads(path.read_text(encoding="utf-8"))
    if damage == "not_object":
        result = []
    elif damage == "missing":
        del result["cache_key"]
    elif damage == "nan":
        result["metrics"]["mean"]["value"] = float("nan")
    else:
        result["channels"] = [True]
    path.write_text(json.dumps(result), encoding="utf-8")
    # Simulate discovering a result copied from another authorized data root.
    with runtime.repository.lock, runtime.repository.db:
        runtime.repository.db.execute("DELETE FROM metric_results")
    with pytest.raises(DeviceError) as rejected:
        service.list_processing_results(client, grant)
    assert rejected.value.code == "result_unavailable"


def test_changed_raw_values_do_not_reuse_result(metrics):
    _, service, client, grant, _, _, calls, _ = metrics
    record_id = capture_record(metrics)
    done = settle(service, client, grant, process_record(metrics, record_id))
    record = service.metrics.store.resolve(record_id)
    path = record.device.folder / "_raw" / "samples.npz"
    with np.load(path, allow_pickle=False) as arrays:
        changed = {name: arrays[name] for name in arrays.files}
    changed["values_0"] = changed["values_0"] + 1.0
    np.savez_compressed(path, **changed)
    second = settle(service, client, grant, process_record(metrics, record_id))
    assert second["state"] == "completed", second
    assert second["result_id"] != done["result_id"] and len(calls) == 2
    old = service.get_processing_result(client, grant, done["result_id"])
    new = service.get_processing_result(client, grant, second["result_id"])
    assert new["metrics"]["mean"]["value"] == pytest.approx(old["metrics"]["mean"]["value"] + 1.0)


def test_invalid_record_metadata_does_not_break_listing(metrics):
    _, service, client, grant, _, _, _, _ = metrics
    record_id = capture_record(metrics)
    record = service.metrics.store.resolve(record_id)
    path = record.device.folder / "_raw" / "recording.json"
    metadata = json.loads(path.read_text(encoding="utf-8"))
    del metadata["kind"]
    path.write_text(json.dumps(metadata), encoding="utf-8")
    listed = service.list_recordings(client, grant)
    assert listed[0]["recording_id"] == record_id and not listed[0]["available"]
    metadata["identity"]["serial"] = "UNKNOWN"
    path.write_text(json.dumps(metadata), encoding="utf-8")
    assert service.list_recordings(client, grant) == []


def test_saved_result_after_restart_without_algorithm(metrics, monkeypatch):
    runtime, service, client, grant, device, _, _, _ = metrics
    record_id = capture_record(metrics)
    done = settle(service, client, grant, process_record(metrics, record_id))
    original = service.get_processing_result(client, grant, done["result_id"])
    root = runtime.repository.root.parent
    service.ledger.insert(
        {
            "id": "interrupted-metric",
            "client_id": client,
            "request_id": "lost",
            "device_id": device,
            "action": "process",
            "state": "running",
        },
        "lost-fingerprint",
    )
    runtime.close()
    monkeypatch.setitem(
        DEVICE_DEFINITIONS, "rs.rtm3004", replace(DEVICE_DEFINITIONS["rs.rtm3004"], metric_profiles=())
    )
    with ApplicationRuntime(root=root, hardware=False) as reopened:
        agent = reopened.agent
        assert agent.ledger.get("interrupted-metric")["state"] == "interrupted"
        renewed = agent.policy.create_grant(
            client, [device], ["view", "results"], channels=[1, 2], operations=[], limits={}
        )
        assert agent.get_processing_result(client, renewed, done["result_id"]) == original


def test_model_finite_capture_and_output_authority(metrics, monkeypatch):
    runtime, service, client, grant, device, profile, calls, _ = metrics
    session = runtime.sessions.sessions[device]
    batch = runtime.sessions.submit(device, "acquire", AcquisitionRequest("waveform", points=32)).result()

    def finite(request, stop):
        yield replace(batch, configuration={**batch.configuration, "complete": False})
        yield replace(batch, configuration={**batch.configuration, "complete": True})

    monkeypatch.setattr(session.adapter, "recorded_run_kinds", ("waveform",), raising=False)
    monkeypatch.setattr(session.adapter, "validate_recorded_run", lambda request: None, raising=False)
    monkeypatch.setattr(session.adapter, "run_acquisition", finite, raising=False)
    definition = DEVICE_DEFINITIONS["rs.rtm3004"]
    finite_profile = replace(
        profile, capture=lambda p: CapturePlan(AcquisitionRequest("waveform", duration=1.0), "finite")
    )
    monkeypatch.setitem(
        DEVICE_DEFINITIONS, "rs.rtm3004", replace(definition, metric_profiles=(finite_profile,))
    )
    done = settle(
        service, client, grant, service.start_experiment(client, grant, device, "finite", "test.mean")
    )
    assert done["state"] == "completed", done
    assert len(calls) == 1
    triggered = replace(profile, capture=lambda p: CapturePlan(AcquisitionRequest("waveform", initiate=True)))
    monkeypatch.setitem(DEVICE_DEFINITIONS, "rs.rtm3004", replace(definition, metric_profiles=(triggered,)))
    with pytest.raises(DeviceError):
        service.start_experiment(client, grant, device, "trigger-denied", "test.mean")
    assert len(calls) == 1


def test_partial_requires_both_declaration_and_request(metrics, monkeypatch):
    _, service, client, grant, _, profile, calls, _ = metrics
    record_id = capture_record(metrics)
    record = service.metrics.store.resolve(record_id)
    path = record.device.folder / "_raw" / "summary.json"
    summary = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**summary, "paused": "test partial"}), encoding="utf-8")
    with pytest.raises(DeviceError, match="不完整"):
        process_record(metrics, record_id)
    with pytest.raises(DeviceError, match="不支持"):
        process_record(metrics, record_id, allow_partial=True)
    monkeypatch.setitem(
        DEVICE_DEFINITIONS,
        "rs.rtm3004",
        replace(DEVICE_DEFINITIONS["rs.rtm3004"], metric_profiles=(replace(profile, supports_partial=True),)),
    )
    done = settle(service, client, grant, process_record(metrics, record_id, allow_partial=True))
    assert done["state"] == "partial", done
    assert not service.get_processing_result(client, grant, done["result_id"])["complete"]
    assert len(calls) == 1
    repeated = settle(service, client, grant, process_record(metrics, record_id, allow_partial=True))
    assert repeated["state"] == "partial" and repeated["result_id"] != done["result_id"]
    assert len(calls) == 2


def test_cancel_automatic_capture_does_not_compute(metrics):
    runtime, service, client, grant, device, _, calls, _ = metrics
    task = service.start_experiment(
        client, grant, device, "long-capture", "test.mean", capture_parameters={"frames": 100}
    )
    deadline = time.monotonic() + 3
    while not runtime.sessions.sessions[device].direct_recording and time.monotonic() < deadline:
        time.sleep(0.01)
    stop = service.stop_task(client, grant, task["id"], "stop-capture")
    assert settle(service, client, grant, stop)["state"] == "completed"
    done = settle(service, client, grant, task)
    assert done["state"] == "cancelled"
    assert not calls
    assert runtime.sessions.sessions[device].direct_recording.binary_done
    records = service.list_recordings(client, grant)
    assert len(records) == 1 and not records[0]["complete"]
    denied = settle(service, client, grant, process_record(metrics, records[0]["recording_id"]))
    assert denied["code"] == "incomplete_recording" and not calls


def test_capture_guard_rejection_does_not_degrade_connected_device(metrics):
    runtime, _, _, _, device, _, _, _ = metrics
    checks = []

    def guard(session):
        checks.append(session)
        if len(checks) == 3:  # Admission, first frame, then revoked before the next read.
            raise DeviceError("test revoked authorization", "permission_denied")

    future = runtime.sessions.start_acquisition(
        device, AcquisitionRequest("waveform", points=32), count=3, interval=0.05, before_io=guard
    )
    with pytest.raises(DeviceError, match="revoked"):
        future.result(10)
    session = runtime.sessions.sessions[device]
    assert session.direct_recording.committed == 1
    assert session.direct_recording.binary_done and session.state == "connected"
    path = session.direct_recording.raw_folder / "acquisition-events.jsonl"
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert events[0]["event"] == "acquisition_started"
    assert events[-1]["event"] == "acquisition_finished" and not events[-1]["complete"]
    # A crash or slow teardown after CSV drains is still unfinished acquisition.
    path.write_text(json.dumps(events[0]) + "\n", encoding="utf-8")
    _, service, client, grant, *_ = metrics
    record = service.list_recordings(client, grant)[0]
    assert not record["available"] and record["code"] == "recording_busy"


def test_tampered_result_is_not_returned(metrics):
    _, service, client, grant, _, _, _, _ = metrics
    record_id = capture_record(metrics)
    done = settle(service, client, grant, process_record(metrics, record_id))
    record = service.metrics.store.resolve(record_id)
    path = record.device.folder / "指标结果" / (done["result_id"] + ".json")
    result = json.loads(path.read_text(encoding="utf-8"))
    result["metrics"]["mean"]["value"] = 123456.0
    path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(DeviceError, match="内容发生冲突"):
        service.get_processing_result(client, grant, done["result_id"])


def test_validation_is_background_and_queued_cancellation_skips_algorithm(metrics, monkeypatch):
    _, service, client, grant, _, _, calls, _ = metrics
    first_record = capture_record(metrics)
    second_record = capture_record(metrics)
    original = service.metrics.store.inspect
    entered, release = threading.Event(), threading.Event()

    def blocked(record, cancel):
        assert threading.current_thread().name.startswith("metric-processing")
        entered.set()
        assert release.wait(5)
        return original(record, cancel)

    monkeypatch.setattr(service.metrics.store, "inspect", blocked)
    first = process_record(metrics, first_record)
    try:
        assert entered.wait(2)
        queued = process_record(metrics, second_record)
        assert service.get_task_status(client, grant, queued["id"])["phase"] == "queued"
        stopped = service.stop_task(client, grant, queued["id"], "cancel-queued")
    finally:
        release.set()
    assert settle(service, client, grant, first)["state"] == "completed"
    assert settle(service, client, grant, stopped)["state"] == "completed"
    assert settle(service, client, grant, queued)["state"] == "cancelled"
    assert calls == [first_record]


def test_mcp_new_tools_flow(metrics):
    from lab_control.agent.connector import remote_client
    from lab_control.agent.server import AgentServer

    runtime, service, owner, grant, device, _, _, token = metrics
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    service.server = AgentServer(service)
    service.server.configure({"enabled": True, "local_port": port})
    config = {"url": f"http://127.0.0.1:{port}/mcp"}

    async def flow():
        async with remote_client(config, True, token) as client:
            tools = (await client.list_tools()).tools
            assert len(tools) == 26
            scope = {"grant_id": grant, "device_id": device}
            result = await client.call_tool("get_processing_capabilities", scope)
            assert result.structured_content["data"]["supported"]
            result = await client.call_tool(
                "start_experiment", {**scope, "request_id": "sdk-metrics", "profile_id": "test.mean"}
            )
            assert not result.is_error, result
            task = result.structured_content["data"]
        # Accepted work survives transport disconnection.
        done = await asyncio.to_thread(settle, service, owner, grant, task)
        async with remote_client(config, True, token) as client:
            result = await client.call_tool(
                "get_processing_result", {"grant_id": grant, "result_id": done["result_id"]}
            )
            assert result.structured_content["data"]["metrics"]["mean"]["unit"] == "V"

    asyncio.run(flow())
    connection = runtime.repository.root / "connector.json"
    service.server.export_connection(connection)
    command = [sys.executable, "-m", "lab_control", "--agent-connector", "--connection", str(connection)]
    environment = {**os.environ, "LAB_CONTROL_AGENT_TOKEN": token, "PYTHONIOENCODING": "ascii"}
    arguments = json.dumps({"grant_id": grant, "device_id": device}).encode("utf-8")
    called = subprocess.run(
        [*command, "--call", "get_processing_capabilities", "--arguments-file", "-"],
        input=arguments,
        env=environment,
        capture_output=True,
        timeout=20,
    )
    assert called.returncode == 0, called.stderr
    response = json.loads(called.stdout.decode("utf-8"))
    assert response["data"]["supported"]
    assert response["data"]["profiles"][0]["description"] == "测试专用指标"
    rejected = subprocess.run(
        [*command, "--call", "get_processing_capabilities", "--arguments-file", "-"],
        input=b"[]",
        env=environment,
        capture_output=True,
        timeout=20,
    )
    assert rejected.returncode == 2 and not json.loads(rejected.stdout)["ok"]
