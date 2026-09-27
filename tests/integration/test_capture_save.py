import threading
from dataclasses import replace

import pytest

from lab_control.domain.instruments import AcquisitionRequest, DeviceError

from .test_monitoring_timing import connected, intercept_recording, wait_for


def test_adapter_diagnostics_survive_session_and_saved_frame_without_overriding_timers(tmp_path):
    runtime, key = connected(tmp_path)
    with runtime:
        adapter = runtime.sessions.sessions[key].adapter
        original = adapter.acquire

        def acquire(request):
            return replace(
                original(request),
                validation="full",
                performance={
                    "configuration_query_mode": "grouped",
                    "logical_queries": 45,
                    "queries": 999,
                    "acquisition_seconds": -1,
                },
            )

        adapter.acquire = acquire
        saved = []
        runtime.repository.export = lambda batch, mode, **options: saved.append(batch) or "saved"
        runtime.monitoring.capture_and_export(key, AcquisitionRequest("spectrum"), "electronic").result(3)
        performance = saved[0].performance
        assert performance["configuration_query_mode"] == "grouped" and performance["logical_queries"] == 45
        assert performance["queries"] != 999 and performance["acquisition_seconds"] >= 0


def test_monitor_uses_live_but_scheduled_saves_require_full(tmp_path):
    runtime, key = connected(tmp_path)
    with runtime:
        adapter = runtime.sessions.sessions[key].adapter
        adapter.supports_live_acquisition = True
        acquire = adapter.acquire
        requested, saved = [], []

        def read(request):
            requested.append(request.consistency)
            return replace(acquire(request), validation=request.consistency)

        adapter.acquire = read
        intercept_recording(runtime, lambda batch, mode: saved.append(batch))
        runtime.monitoring.start(key, (AcquisitionRequest("spectrum"),), interval=0.05, record_interval=0.2)
        wait_for(lambda: "live" in requested)
        runtime.monitoring.set_recording(key, True)
        wait_for(lambda: len(saved) >= 2)
        runtime.monitoring.stop(key).result(3)
        assert all(b.validation == "full" for b in saved)
        assert requested.count("live") > requested.count("full")


def test_manual_save_queues_fresh_full_read_and_blocks_double_click(tmp_path):
    runtime, key = connected(tmp_path)
    with runtime:
        adapter = runtime.sessions.sessions[key].adapter
        adapter.supports_live_acquisition = True
        original = adapter.acquire
        entered, release = threading.Event(), threading.Event()
        requests, saved = [], []

        def acquire(request):
            requests.append(request)
            if len(requests) == 1:
                entered.set()
                assert release.wait(3)
            return replace(original(request), validation=request.consistency)

        adapter.acquire = acquire
        runtime.repository.export = lambda batch, mode, **options: saved.append(batch) or "saved"
        service = runtime.monitoring
        service.start(key, (AcquisitionRequest("spectrum"),), interval=0.1)
        assert entered.wait(2)
        future = service.capture_and_export(
            key, AcquisitionRequest("spectrum", initiate=True, confirmed=True), "electronic"
        )
        with pytest.raises(DeviceError, match="正在进行"):
            service.capture_and_export(key, AcquisitionRequest("spectrum"), "electronic")
        release.set()
        assert future.result(3) == "saved"
        service.stop(key).result(3)
        assert requests[0].consistency == "live"
        assert requests[1].consistency == "full" and not requests[1].initiate
        assert len(saved) == 1 and saved[0].validation == "full"


def test_manual_failure_does_not_export_previous_frame_or_retry(tmp_path):
    runtime, key = connected(tmp_path)
    with runtime:
        service = runtime.monitoring
        service.start(key, (AcquisitionRequest("spectrum"),), once=True)
        wait_for(lambda: service.latest(key, "spectrum") is not None)
        calls, saved = [], []

        def fail(request):
            calls.append(request)
            raise DeviceError("truncated", "parse_error")

        runtime.sessions.sessions[key].adapter.acquire = fail
        runtime.repository.export = lambda batch, mode, **options: saved.append(batch)
        future = service.capture_and_export(key, AcquisitionRequest("spectrum"), "electronic")
        with pytest.raises(DeviceError):
            future.result(3)
        assert len(calls) == 1 and not saved


def test_unsupported_adapter_keeps_full_request_and_close_drains_manual_save(tmp_path):
    runtime, key = connected(tmp_path)
    adapter = runtime.sessions.sessions[key].adapter
    original = adapter.acquire
    received = []
    adapter.acquire = lambda request: received.append(request.consistency) or original(request)
    runtime.sessions.submit(key, "acquire", AcquisitionRequest("spectrum", consistency="live")).result(3)
    future = runtime.monitoring.capture_and_export(key, AcquisitionRequest("spectrum"), "electronic")
    runtime.close()
    assert future.done() and future.result().exists()
    assert received == ["full", "full"]
