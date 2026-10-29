import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.demo.instrument import SimulatedInstrument
from lab_control.discovery.service import DiscoveryCandidate
from lab_control.domain.instruments import DeviceError


@pytest.fixture
def runtime(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as app:
        yield app


def discovered(runtime, name, adapter="rs.sma100b"):
    config = ConnectionConfig(f"mock://{name}", adapter, "mock", expected_serial=name)
    identity = SimulatedInstrument(config).identify()
    record = runtime.inventory.merge(DiscoveryCandidate(config, "configured"), "scan", identity, adapter)
    return record["id"], config, identity


def slow_open(runtime, monkeypatch, *, count=1):
    entered, release = threading.Event(), threading.Event()
    opened = []

    class Slow(SimulatedInstrument):
        def identify(self):
            assert release.wait(5)
            return super().identify()

    def factory(config):
        opened.append(config.resource)
        if len(opened) >= count:
            entered.set()
        return Slow(config)

    monkeypatch.setattr(runtime.sessions, "factory", factory)
    return entered, release, opened


def test_batch_uses_current_unique_assets_preserves_sessions_and_continues_failures(runtime, monkeypatch):
    first, cfg, identity = discovered(runtime, "first")
    alias = replace(cfg, resource="mock://alias")
    runtime.inventory.merge(DiscoveryCandidate(alias, "configured"), "scan", identity, cfg.adapter)
    live, _, _ = discovered(runtime, "live")
    runtime.connect(live).result(3)
    session = runtime.sessions.sessions[live]
    broken, _, _ = discovered(runtime, "broken")
    last, _, _ = discovered(runtime, "last", "thorlabs.pm100d")
    historical = runtime.add(ConnectionConfig("mock://not-seen", "rs.sma100b", "mock"))
    unknown = runtime.inventory.merge(
        DiscoveryCandidate(ConnectionConfig("mock://unknown", transport="mock"), "serial"), "scan"
    )["id"]
    removed, _, _ = discovered(runtime, "removed")
    runtime.inventory.records[removed]["adapterId"] = "missing.model"
    runtime.inventory.records[removed]["config"]["adapter"] = "missing.model"
    conflict, _, _ = discovered(runtime, "conflict")
    runtime.inventory.records[conflict]["identityConflict"] = {"reason": "fixture"}
    opened = []

    def factory(config):
        opened.append(config.resource)
        if config.expected_serial == "broken":
            raise DeviceError("fixture driver missing", "driver_missing")
        return SimulatedInstrument(config)

    monkeypatch.setattr(runtime.sessions, "factory", factory)
    result = runtime.connect_all_discovered().result(5)
    items = {item["device_id"]: item for item in result["items"]}
    assert result["state"] == "completed" and result["total"] == result["completed"] == 7
    assert items[first]["status"] == items[last]["status"] == "connected"
    assert items[live]["status"] == "already_connected"
    assert items[broken]["status"] == "failed" and items[broken]["error_code"] == "driver_missing"
    assert all(items[key]["status"] == "skipped" for key in (unknown, removed, conflict))
    assert historical not in items
    assert sorted(opened) == ["mock://broken", "mock://first", "mock://last"]
    assert runtime.sessions.sessions[live] is session
    assert all(not s.monitoring and not s.recording for s in runtime.sessions.sessions.values())
    result["items"].clear()
    assert len(runtime.connection_progress()["items"]) == 7


def test_cancel_waits_for_inflight_and_admission_blocks_scan_and_manual_connections(runtime, monkeypatch):
    first, config, _ = discovered(runtime, "first")
    second, _, _ = discovered(runtime, "second")
    for index in range(4):
        discovered(runtime, f"queued-{index}")
    entered, release, opened = slow_open(runtime, monkeypatch, count=4)
    future = runtime.connect_all_discovered()
    try:
        assert entered.wait(2)
        assert runtime.connection_progress()["current"] and not future.done()
        for operation in (
            runtime.connect_all_discovered,
            runtime.discovery.scan,
            lambda: runtime.connect(second),
            lambda: runtime.select_resource(first, config.resource),
        ):
            with pytest.raises(DeviceError) as error:
                operation()
            assert error.value.code == "connection_batch_busy"
        discovered(runtime, "late-arrival")
        runtime.cancel_connect_all()
        assert runtime.connection_progress()["state"] == "stopping" and not future.done()
    finally:
        release.set()
    result = future.result(3)
    assert result["state"] == "cancelled" and result["total"] == 6
    assert [item["status"] for item in result["items"]] == ["connected"] * 4 + ["skipped"] * 2
    assert len(opened) == 4
    assert runtime.sessions.snapshots()[0]["connectionState"] == "connected"
    assert runtime.connect_all_discovered().result(3)["total"] == 7


def test_batch_rejected_during_scan(runtime):
    discovered(runtime, "fixture")
    entered, release = threading.Event(), threading.Event()

    class Provider:
        name = "test"

        def enumerate(self):
            entered.set()
            assert release.wait(5)
            return ()

    runtime.discovery.providers = (Provider(),)
    scan = runtime.discovery.scan()
    try:
        assert entered.wait(2)
        with pytest.raises(DeviceError) as error:
            runtime.connect_all_discovered()
        assert error.value.code == "scan_busy"
    finally:
        release.set()
    scan.result(3)


def test_shutdown_cancels_remaining_and_drains_before_closing_repository(runtime, monkeypatch):
    for index in range(6):
        discovered(runtime, f"device-{index}")
    entered, release, opened = slow_open(runtime, monkeypatch, count=4)
    future = runtime.connect_all_discovered()
    assert entered.wait(2)
    cancelled = threading.Event()
    cancel = runtime.bulk_connection.cancel

    def notify_cancel():
        cancel()
        cancelled.set()

    monkeypatch.setattr(runtime.bulk_connection, "cancel", notify_cancel)
    with ThreadPoolExecutor(max_workers=1) as worker:
        shutdown = worker.submit(runtime.close)
        try:
            assert cancelled.wait(2)
            assert not shutdown.done()
            with pytest.raises(DeviceError, match="退出"):
                runtime.discovery.scan()
            with pytest.raises(DeviceError, match="退出"):
                runtime.connect_all_discovered()
        finally:
            release.set()
        shutdown.result(3)
    assert future.result()["state"] == "cancelled"
    assert len(opened) == 4 and runtime.closed


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.005)


def test_parallel_batch_bypasses_slow_first_device_and_replenishes_free_slots(runtime, monkeypatch):
    keys = [discovered(runtime, name)[0] for name in ("slow", "quick-1", "quick-2", "quick-3", "quick-4")]
    entered, release = threading.Event(), threading.Event()
    calls = []

    class Adapter(SimulatedInstrument):
        def identify(self):
            calls.append(self.config.expected_serial)
            if self.config.expected_serial == "slow":
                entered.set()
                assert release.wait(5)
                raise DeviceError("frequency report timeout", "timeout")
            return super().identify()

    monkeypatch.setattr(runtime.sessions, "factory", Adapter)
    future = runtime.connect_all_discovered()
    try:
        assert entered.wait(2)
        wait_for(lambda: runtime.connection_progress()["completed"] == 4)
        progress = runtime.connection_progress()
        assert not future.done()
        assert [item["device_id"] for item in progress["active"]] == [keys[0]]
        assert progress["queued"] == 0
        assert all(item["status"] == "connected" for item in progress["items"])
        assert len(calls) == len(set(calls)) == 5
    finally:
        release.set()
    result = future.result(3)
    assert [item["device_id"] for item in result["items"]] == keys
    assert [item["status"] for item in result["items"]] == ["failed"] + ["connected"] * 4
    assert result["active"] == [] and result["current"] == ""


def test_batch_never_admits_more_than_four_devices_and_cancel_skips_queue(runtime, monkeypatch):
    for index in range(9):
        discovered(runtime, f"bounded-{index}")
    entered, release, opened = slow_open(runtime, monkeypatch, count=4)
    future = runtime.connect_all_discovered()
    try:
        assert entered.wait(2)
        wait_for(lambda: len(runtime.connection_progress()["active"]) == 4)
        assert len(opened) == 4
        assert runtime.connection_progress()["queued"] == 5
        runtime.cancel_connect_all()
        wait_for(lambda: runtime.connection_progress()["completed"] == 5)
        assert len(opened) == 4
        assert not future.done()
        assert runtime.discovery.connection_batch_active
    finally:
        release.set()
    result = future.result(3)
    assert result["completed"] == 9 and result["state"] == "cancelled"
    assert [item["status"] for item in result["items"]] == ["connected"] * 4 + ["skipped"] * 5


def test_slow_connection_preparation_does_not_hold_progress_or_admission_lock(runtime, monkeypatch):
    slow, _, _ = discovered(runtime, "slow-preparation")
    discovered(runtime, "quick")
    entered, release = threading.Event(), threading.Event()
    connect = runtime._connect

    def prepare(key, **kwargs):
        if key == slow:
            entered.set()
            assert release.wait(5)
        return connect(key, **kwargs)

    monkeypatch.setattr(runtime, "_connect", prepare)
    future = runtime.connect_all_discovered()
    try:
        assert entered.wait(2)
        # The UI uses this lock for progress/scan status and the Stop button.
        acquired = runtime.discovery.lock.acquire(timeout=0.5)
        if acquired:
            runtime.discovery.lock.release()
        assert acquired
        wait_for(lambda: runtime.connection_progress()["completed"] == 1)
        assert runtime.connection_progress()["active"][0]["device_id"] == slow
        runtime.cancel_connect_all()
        assert runtime.connection_progress()["state"] == "stopping"
        assert not future.done()
    finally:
        release.set()
    assert [item["status"] for item in future.result(3)["items"]] == ["connected"] * 2


def test_unexpected_audit_failure_still_drains_admitted_connections(runtime, monkeypatch):
    for name in ("quick", "slow"):
        discovered(runtime, name)
    entered, release = threading.Event(), threading.Event()
    audit_failed = threading.Event()
    audit = runtime.repository.audit

    class Adapter(SimulatedInstrument):
        def identify(self):
            if self.config.expected_serial == "slow":
                entered.set()
                assert release.wait(5)
            return super().identify()

    def fail_audit(event, **details):
        if event == "bulk_connection_item":
            audit_failed.set()
            raise OSError("audit disk unavailable")
        audit(event, **details)

    monkeypatch.setattr(runtime.sessions, "factory", Adapter)
    monkeypatch.setattr(runtime.repository, "audit", fail_audit)
    future = runtime.connect_all_discovered()
    try:
        assert entered.wait(2) and audit_failed.wait(2)
        assert runtime.discovery.connection_batch_active and not future.done()
        wait_for(lambda: runtime.connection_progress()["state"] == "stopping")
    finally:
        release.set()
    with pytest.raises(OSError, match="audit disk"):
        future.result(3)
    assert not runtime.discovery.connection_batch_active
    assert runtime.connection_progress()["state"] == "error"
    assert all(session.identity for session in runtime.sessions.sessions.values())


def test_batch_respects_existing_resource_lease_and_does_not_retry_error_session(runtime):
    first, config, _ = discovered(runtime, "occupied")
    second, _, _ = discovered(runtime, "available")
    runtime.leases.acquire("another-task", (config.resource,))
    try:
        result = runtime.connect_all_discovered().result(3)
        assert [item["status"] for item in result["items"]] == ["failed", "connected"]
        assert result["items"][0]["error_code"] == "resource_busy"
        assert runtime.leases.owner(config.resource) == "another-task"
    finally:
        runtime.leases.release("another-task")
    runtime.sessions.sessions[second].state = "error"
    result = runtime.connect_all_discovered().result(3)
    assert result["items"][0]["device_id"] == first
    assert [item["status"] for item in result["items"]] == ["connected", "skipped"]


def test_completed_connection_future_includes_persisted_identity(runtime):
    key = runtime.add(ConnectionConfig("mock://new", "rs.sma100b", "mock"))
    identity = runtime.connect(key).result(3)
    saved = next(r for r in runtime.repository.records() if r["id"] == key)
    assert saved["identityKey"] == identity.key


def test_empty_batch_is_successful(runtime):
    result = runtime.connect_all_discovered().result(3)
    assert result["state"] == "completed" and result["items"] == [] and result["total"] == 0


def test_cancel_during_final_audit_does_not_leave_progress_stuck_stopping(runtime, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    audit = runtime.repository.audit

    def slow_audit(event, **details):
        if event == "bulk_connection_finished":
            entered.set()
            assert release.wait(5)
        audit(event, **details)

    monkeypatch.setattr(runtime.repository, "audit", slow_audit)
    future = runtime.connect_all_discovered()
    try:
        assert entered.wait(2)
        runtime.cancel_connect_all()
        assert runtime.connection_progress()["state"] == "completed"
    finally:
        release.set()
    assert future.result(3)["state"] == "completed"


def test_degraded_open_is_reported_as_failed_and_next_device_still_connects(runtime, monkeypatch):
    discovered(runtime, "degraded")
    discovered(runtime, "healthy")
    refresh = runtime.sessions._refresh_capabilities

    def failing_refresh(session):
        if session.config.expected_serial == "degraded":
            raise DeviceError("fixture capability timeout", "timeout")
        return refresh(session)

    monkeypatch.setattr(runtime.sessions, "_refresh_capabilities", failing_refresh)
    result = runtime.connect_all_discovered().result(3)
    assert [item["status"] for item in result["items"]] == ["failed", "connected"]
    assert "fixture capability timeout" in result["items"][0]["reason"]
