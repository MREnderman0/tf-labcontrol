"""Verify remote processing with registered and test-only profiles, without physical I/O."""

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

from lab_control.application.agents.policy import INTERNAL_CLIENT
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
from lab_control.domain.optical_power_metrics import (
    OPTICAL_POWER_FLUCTUATION_PROFILE,
    OPTICAL_POWER_REFERENCE_DEVIATION_PROFILE,
)
from lab_control.domain.phase_noise_metrics import PHASE_NOISE_AT_OFFSET_PROFILE
from lab_control.domain.spectrum_metrics import SPECTRUM_PEAK_AND_SNR_PROFILE
from lab_control.domain.voltage_waveform_metrics import VOLTAGE_WAVEFORM_STATISTICS_PROFILE
from lab_control.instruments.registry import DEVICE_DEFINITIONS
from lab_control.storage.agent_ledger import ACTIVE
from lab_control.storage.processing import iter_source_frames


class Parameters(MetricParameters):
    scale: float = 1.0


class CaptureParameters(MetricParameters):
    frames: int = 1


def settle(service, client, grant, task, timeout=12, *, local_commands=False):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = service.get_task_status(client, grant, task["id"], local_commands=local_commands)
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
    client = INTERNAL_CLIENT
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
    yield runtime, service, client, grant, device, profile, calls
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


def test_unrelated_device_models_do_not_publish_metrics():
    for adapter in ("siglent.sdg6032xe", "ceyear.6362d", "rigol.m300"):
        assert not DEVICE_DEFINITIONS[adapter].metric_profiles


@pytest.mark.parametrize(("adapter", "channels"), [("thorlabs.pm100d", [1]), ("joinwit.jw8105", [1, 9])])
def test_per_channel_metrics_use_saved_optical_power_channels(metrics, adapter, channels):
    runtime, service, client, *_ = metrics
    device = runtime.add(ConnectionConfig("mock://metrics/power", adapter, "mock"))
    runtime.connect(device).result()
    grant = service.policy.create_grant(
        client,
        [device],
        ["view", "acquire", "results", "process"],
        channels=channels,
        operations=[],
        limits={},
    )
    profiles = service.get_processing_capabilities(client, grant, device)["profiles"]
    assert {profile["id"] for profile in profiles} == {
        OPTICAL_POWER_FLUCTUATION_PROFILE.id,
        OPTICAL_POWER_REFERENCE_DEVIATION_PROFILE.id,
    }
    assert all(profile["per_channel_metrics"] for profile in profiles)
    acquired = settle(
        service,
        client,
        grant,
        service.acquire_once(
            client, grant, device, {"kind": "scalar", "channels": channels}, "power-capture"
        ),
    )
    assert acquired["state"] == "completed", acquired

    processed = settle(
        service,
        client,
        grant,
        service.start_processing(
            client,
            grant,
            device,
            "power-deviation",
            acquired["recording_id"],
            OPTICAL_POWER_REFERENCE_DEVIATION_PROFILE.id,
            parameters={"reference_power_w": {str(channel): 0.001 for channel in channels}},
        ),
    )
    assert processed["state"] == "completed", processed
    result = service.get_processing_result(client, grant, processed["result_id"])
    assert result["channels"] == channels
    assert set(result["metrics"]) == {
        f"ch{channel}.{name}"
        for channel in channels
        for name in ("min_deviation_percent", "max_deviation_percent")
    }
    for channel in channels:
        assert (
            result["metrics"][f"ch{channel}.min_deviation_percent"]
            == result["metrics"][f"ch{channel}.max_deviation_percent"]
        )
        assert result["metrics"][f"ch{channel}.min_deviation_percent"]["unit"] == "%"
        assert result["metrics"][f"ch{channel}.min_deviation_percent"]["quality"] == "simulated"


@pytest.mark.parametrize(
    "adapter",
    [
        "rs.rtm3004",
        "rigol.dho924",
        "rs.rto64",
        "rigol.ds1202ze",
        "lecroy.9254",
        "lecroy.9404m",
        "pico.6404e",
    ],
)
def test_voltage_metrics_use_full_saved_waveform(tmp_path, adapter):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        device = runtime.add(ConnectionConfig("mock://metrics/voltage", adapter, "mock"))
        runtime.connect(device).result()
        service, client = runtime.agent, INTERNAL_CLIENT
        grant = service.policy.create_grant(
            client,
            [device],
            ["view", "acquire", "results", "process", "output_trigger"],
            channels=[1, 2],
            operations=[],
            limits={},
        )
        record_id = capture_record((runtime, service, client, grant, device), (1, 2))
        task = service.start_processing(
            client, grant, device, "voltage-statistics", record_id, VOLTAGE_WAVEFORM_STATISTICS_PROFILE.id
        )
        done = settle(service, client, grant, task)
        assert done["state"] == "completed", done
        result = service.get_processing_result(client, grant, done["result_id"])
        record = service.metrics.store.resolve(record_id)
        frame = next(iter_source_frames(record.device))
        assert result["channels"] == [1, 2]
        assert result["profile_version"] == "2"
        for measurement in frame["measurements"]:
            channel = measurement["channel"]
            values = measurement["values"].astype(np.float64)
            assert len(values) == 32
            for name, expected in {
                "mean": np.mean(values),
                "peak_to_peak": np.ptp(values),
                "rms": np.sqrt(np.mean(np.square(values))),
                "std": np.std(values, ddof=0),
            }.items():
                assert result["metrics"][f"ch{channel}.{name}"] == {
                    "value": pytest.approx(expected),
                    "unit": "V",
                    "quality": "simulated",
                }


@pytest.mark.parametrize(
    ("adapter", "source_key", "profile", "parameters", "metric", "unit"),
    [
        ("rs.fph", "spectrum", SPECTRUM_PEAK_AND_SNR_PROFILE, {}, "peak_power", "dBm"),
        ("rigol.dsa815", "spectrum", SPECTRUM_PEAK_AND_SNR_PROFILE, {}, "peak_power", "dBm"),
        (
            "microchip.53100a",
            "phase_noise",
            PHASE_NOISE_AT_OFFSET_PROFILE,
            {"offset_hz": 10.0},
            "phase_noise",
            "dBc/Hz",
        ),
    ],
)
def test_registered_trace_metrics(metrics, adapter, source_key, profile, parameters, metric, unit):
    runtime, service, client, *_ = metrics
    device = runtime.add(ConnectionConfig("mock://metrics/trace", adapter, "mock"))
    runtime.connect(device).result()
    grant = service.policy.create_grant(
        client, [device], ["view", "acquire", "results", "process"], channels=[1], operations=[], limits={}
    )
    sources = service.get_capabilities(client, grant, device)["data_sources"]
    source = next(item for item in sources if item["key"] == source_key)
    acquired = settle(
        service,
        client,
        grant,
        service.acquire_once(
            client,
            grant,
            device,
            {"kind": source["kind"], "quantity": source["quantity"], "points": 64},
            "trace-capture",
        ),
    )
    assert acquired["state"] == "completed", acquired
    task = service.start_processing(
        client, grant, device, "trace-metrics", acquired["recording_id"], profile.id, parameters=parameters
    )
    done = settle(service, client, grant, task)
    assert done["state"] == "completed", done
    result = service.get_processing_result(client, grant, done["result_id"])
    assert result["metrics"][metric]["value"] is not None
    assert result["metrics"][metric]["unit"] == unit
    assert result["metrics"][metric]["quality"] == "simulated"


def test_steps_cache_force_versions_and_no_duplicate_arrays(metrics, monkeypatch):
    runtime, service, client, grant, device, profile, calls = metrics
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
    runtime, service, client, grant, device, _, calls = metrics
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


def test_saved_experiment_preserves_record_order_and_rechecks_all_sources(metrics, monkeypatch):
    runtime, service, client, grant, device, profile, calls = metrics
    adapter = "rs.rtm3004"
    profile = replace(profile, supports_multiple_recordings=True)
    definition = replace(DEVICE_DEFINITIONS[adapter], metric_profiles=(profile,))
    monkeypatch.setitem(DEVICE_DEFINITIONS, adapter, definition)
    older = capture_record(metrics)
    newer = capture_record(metrics)
    ids = [newer, older]

    def process(request_id):
        task = service.start_experiment(client, grant, device, request_id, profile.id, recording_ids=ids)
        done = settle(service, client, grant, task)
        assert done["state"] == "completed", done
        assert done["recording_ids"] == ids
        return done

    first = process("saved-series")
    result = service.get_processing_result(client, grant, first["result_id"])
    assert result["recording_id"] == newer
    assert result["recording_ids"] == ids
    reused = process("saved-series-retry")
    assert reused["result"]["reused"] and reused["result_id"] == first["result_id"]

    path = service.metrics.store.resolve(older).device.folder / "_raw" / "samples.npz"
    with np.load(path, allow_pickle=False) as arrays:
        changed = {name: arrays[name] for name in arrays.files}
    changed["values_0"] = changed["values_0"] + 2.0
    np.savez_compressed(path, **changed)
    second = process("saved-series-changed")
    updated = service.get_processing_result(client, grant, second["result_id"])
    assert second["result_id"] != first["result_id"] and calls == [newer, newer]
    assert updated["metrics"]["mean"]["value"] == pytest.approx(result["metrics"]["mean"]["value"] + 1.0)
    assert not runtime.repository.record_access.held


def test_saved_experiment_checks_each_record_channel_before_processing(metrics, monkeypatch):
    runtime, service, client, grant, device, profile, calls = metrics
    adapter = "rs.rtm3004"
    profile = replace(profile, supports_multiple_recordings=True)
    definition = replace(DEVICE_DEFINITIONS[adapter], metric_profiles=(profile,))
    monkeypatch.setitem(DEVICE_DEFINITIONS, adapter, definition)
    ids = [capture_record(metrics, (1,)), capture_record(metrics, (2,))]
    with pytest.raises(DeviceError, match="无重复"):
        service.start_experiment(
            client, grant, device, "duplicate-record", profile.id, recording_ids=ids[:1] * 2
        )
    with pytest.raises(DeviceError, match="只能选一种"):
        service.start_experiment(
            client, grant, device, "two-input-modes", profile.id, capture_parameters={}, recording_ids=ids
        )
    limited = service.policy.create_grant(
        client, [device], ["view", "results", "process"], channels=[1], operations=[], limits={}
    )
    task = service.start_experiment(client, limited, device, "denied-series", profile.id, recording_ids=ids)
    denied = settle(service, client, limited, task)
    assert denied["state"] == "failed" and denied["code"] == "permission_denied"
    assert calls == [] and not runtime.repository.record_access.held


def test_channels_grant_and_parameter_rejection_before_capture(metrics):
    runtime, service, client, grant, device, _, calls = metrics
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
    runtime, service, client, grant, device, _, calls = metrics
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
    _, service, client, grant, _, _, calls = metrics
    record_id = capture_record(metrics)
    done = settle(service, client, grant, process_record(metrics, record_id, request_id="same"))
    with pytest.raises(DeviceError, match="request_id"):
        process_record(metrics, record_id, request_id="same", parameters={"scale": 2.0})
    changed = settle(service, client, grant, process_record(metrics, record_id, parameters={"scale": 2.0}))
    assert changed["result_id"] != done["result_id"] and len(calls) == 2


def test_cancel_join_queue_and_file_reservation(metrics, monkeypatch):
    runtime, service, client, grant, _, profile, _ = metrics
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
    _, service, client, grant, _, profile, _ = metrics
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
    runtime, service, client, grant, _, _, calls = metrics
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
    _, service, client, grant, _, profile, _ = metrics
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
    runtime, service, client, grant, _, _, _ = metrics
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
    _, service, client, grant, _, _, calls = metrics
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
    _, service, client, grant, _, _, _ = metrics
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
    runtime, service, client, grant, device, _, _ = metrics
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
    runtime, service, client, grant, device, profile, calls = metrics
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
    _, service, client, grant, _, profile, calls = metrics
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
    runtime, service, client, grant, device, _, calls = metrics
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
    runtime, _, _, _, device, _, _ = metrics
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
    _, service, client, grant, _, _, _ = metrics
    record_id = capture_record(metrics)
    done = settle(service, client, grant, process_record(metrics, record_id))
    record = service.metrics.store.resolve(record_id)
    path = record.device.folder / "指标结果" / (done["result_id"] + ".json")
    result = json.loads(path.read_text(encoding="utf-8"))
    result["metrics"]["mean"]["value"] = 123456.0
    path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(DeviceError, match="内容发生冲突"):
        service.get_processing_result(client, grant, done["result_id"])


@pytest.mark.parametrize("full_scope", [False, True])
def test_validation_is_background_and_queued_cancellation_skips_algorithm(metrics, monkeypatch, full_scope):
    _, service, client, limited, device, _, calls = metrics
    grant = service.grant_all_devices()[0] if full_scope else limited
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
    first = service.start_processing(client, grant, device, "process-first", first_record, "test.mean")
    try:
        assert entered.wait(2)
        queued = service.start_processing(client, grant, device, "process-queued", second_record, "test.mean")
        assert service.get_task_status(client, grant, queued["id"])["phase"] == "queued"
        if full_scope:
            with pytest.raises(DeviceError, match="通道"):
                service.get_task_status(client, limited, queued["id"])
            assert queued["id"] not in {t["id"] for t in service.list_tasks(client, limited)}
            with pytest.raises(DeviceError, match="通道"):
                service.stop_task(client, limited, queued["id"], "stop-with-limited-grant")
        stopped = service.stop_task(client, grant, queued["id"], "cancel-queued")
    finally:
        release.set()
    assert settle(service, client, grant, first)["state"] == "completed"
    assert service.get_task_status(client, limited, first["id"])["state"] == "completed"
    assert settle(service, client, grant, stopped)["state"] == "completed"
    assert settle(service, client, grant, queued)["state"] == "cancelled"
    assert calls == [first_record]


def test_mcp_new_tools_flow(metrics):
    from lab_control.agent.connector import remote_client
    from lab_control.agent.server import AgentServer

    runtime, service, owner, grant, device, _, _ = metrics
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    service.server = AgentServer(service)
    service.server.configure({"enabled": True, "local_port": port})
    config = {"url": f"http://127.0.0.1:{port}/mcp"}

    async def flow():
        async with remote_client(config, True) as client:
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
        async with remote_client(config, True) as client:
            result = await client.call_tool(
                "get_processing_result", {"grant_id": grant, "result_id": done["result_id"]}
            )
            assert result.structured_content["data"]["metrics"]["mean"]["unit"] == "V"
            result = await client.call_tool(
                "start_experiment",
                {
                    **scope,
                    "request_id": "sdk-saved-metrics",
                    "profile_id": "test.mean",
                    "recording_ids": [done["recording_id"]],
                },
            )
            assert not result.is_error, result
            saved_task = result.structured_content["data"]
        saved = await asyncio.to_thread(settle, service, owner, grant, saved_task)
        assert saved["result"]["reused"] and saved["result_id"] == done["result_id"]

    asyncio.run(flow())
    connection = runtime.repository.root / "connector.json"
    service.server.export_connection(connection)
    command = [sys.executable, "-m", "lab_control", "--agent-connector", "--connection", str(connection)]
    environment = {**os.environ, "PYTHONIOENCODING": "ascii"}
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


def test_command_string_experiment_through_local_and_lab_mcp(metrics, tmp_path):
    from lab_control.agent.connector import connection, remote_client
    from lab_control.agent.server import AgentServer

    runtime, service, owner, grant, device, *_ = metrics
    first = capture_record(metrics)
    second = capture_record(metrics)
    ordinary = settle(service, owner, grant, process_record(metrics, first))
    assert ordinary["state"] == "completed", ordinary
    script = tmp_path / "process with spaces.py"
    script.write_text(
        """
import json
import sys

request = json.load(sys.stdin)
source = request["source"]
assert source["complete"]
assert source["recording_ids"] == request["parameters"]["expected"]
assert source["metadata"]["adapter_id"] == "rs.rtm3004"
assert sys.argv[1] == "argument with spaces"
if len(sys.argv) > 2:
    print("not JSON")
else:
    points = sum(len(frame["measurements"][0]["values"]) for frame in source["frames"])
    print(json.dumps({"metrics": {"points": {"value": points, "unit": "samples", "quality": "good"}}}))
""",
        encoding="utf-8",
    )
    command = subprocess.list2cmdline([sys.executable, str(script), "argument with spaces"])
    arguments = {
        "device_id": device,
        "request_id": "command-test",
        "profile_id": "local.command",
        "command": command,
        "recording_ids": [second, first],
        "parameters": {"expected": [second, first]},
    }
    with socket.socket() as local, socket.socket() as tls:
        local.bind(("127.0.0.1", 0))
        tls.bind(("127.0.0.1", 0))
        local_port, tls_port = local.getsockname()[1], tls.getsockname()[1]
    server = service.server = AgentServer(service)
    server.configure({"enabled": True, "local_port": local_port, "local_commands": True})
    assert "local_commands" not in service.repository.load_workspace("agent/server")
    server.close()
    assert not server.local_commands
    server.settings.update(lan_enabled=True, lan_address="127.0.0.1", lan_port=tls_port)
    server.start()
    exported = tmp_path / "command-https.json"
    server.export_connection(exported, lan=True)
    lan_config, lan_verify = connection(exported)
    local_config = {"url": f"http://127.0.0.1:{local_port}/mcp"}

    async def flow():
        async with remote_client(local_config, True) as local, remote_client(lan_config, lan_verify) as lan:
            denied = await lan.call_tool("start_experiment", arguments)
            assert denied.is_error and denied.structured_content["code"] == "local_command_disabled"
            server.local_commands = True
            capabilities = await lan.call_tool("get_processing_capabilities", {"device_id": device})
            (profile,) = capabilities.structured_content["data"]["profiles"]
            assert profile["id"] == "local.command" and profile["requires_command"]
            for tool, query in (
                ("get_task_status", {"task_id": ordinary["id"]}),
                ("get_processing_result", {"result_id": ordinary["result_id"]}),
                ("stop_task", {"task_id": ordinary["id"], "request_id": "stop-ordinary"}),
                (
                    "start_experiment",
                    {
                        "device_id": device,
                        "profile_id": "test.mean",
                        "recording_ids": [first],
                        "request_id": "ordinary-without-grant",
                    },
                ),
            ):
                denied = await lan.call_tool(tool, query)
                assert denied.is_error and denied.structured_content["code"] == "grant_expired"
            submitted = await lan.call_tool("start_experiment", arguments)
            assert not submitted.is_error, submitted
            task = submitted.structured_content["data"]
            duplicate = await local.call_tool("start_experiment", arguments | {"grant_id": grant})
            assert duplicate.structured_content["data"]["id"] == task["id"]
            done = await asyncio.to_thread(settle, service, owner, "", task, local_commands=True)
            assert done["state"] == "completed", done
            status = await lan.call_tool("get_task_status", {"task_id": task["id"]})
            assert status.structured_content["data"]["result_id"] == done["result_id"]
            result = await lan.call_tool("get_processing_result", {"result_id": done["result_id"]})
            assert result.structured_content["data"]["metrics"] == {
                "points": {"value": 64, "unit": "samples", "quality": "simulated"}
            }
            repeated = await local.call_tool("start_experiment", arguments | {"request_id": "command-again"})
            repeated_task = repeated.structured_content["data"]
            again = await asyncio.to_thread(settle, service, owner, "", repeated_task, local_commands=True)
            assert again["state"] == "completed" and again["result_id"] != done["result_id"]
            malformed = await local.call_tool(
                "start_experiment",
                arguments | {"request_id": "command-malformed", "command": command + " malformed"},
            )
            failure = await asyncio.to_thread(
                settle, service, owner, "", malformed.structured_content["data"], local_commands=True
            )
            assert failure["state"] == "failed" and failure["code"] == "command_invalid_output"

    asyncio.run(flow())


def test_stop_waiting_command(metrics, tmp_path):
    _, service, client, grant, device, *_ = metrics
    record = capture_record(metrics)
    ready = tmp_path / "command-ready"
    program = (
        "import json, sys, time; from pathlib import Path; json.load(sys.stdin); "
        "Path(sys.argv[1]).touch(); time.sleep(60)"
    )
    command = subprocess.list2cmdline([sys.executable, "-c", program, str(ready)])
    task = service.start_experiment(
        client,
        "",
        device,
        "wait-command",
        "local.command",
        recording_ids=[record],
        command=command,
        local_commands=True,
    )
    deadline = time.monotonic() + 8
    while not ready.is_file() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ready.is_file()
    stopped = service.stop_task(client, "", task["id"], "stop-command", local_commands=True)
    assert settle(service, client, "", stopped, local_commands=True)["state"] == "completed"
    assert settle(service, client, "", task, local_commands=True)["state"] == "cancelled"
    assert service.list_processing_results(client, grant, profile_id="local.command") == []
