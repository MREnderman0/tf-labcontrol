import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.application.settings_sync import SettingsSync
from lab_control.domain.instruments import (
    AcquisitionRequest,
    DeviceCommand,
    DeviceError,
    Measurement,
    MeasurementBatch,
    Quality,
)


def wait_for(predicate, timeout=6):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Synchronization did not settle")


def add(runtime, index=0):
    key = runtime.add(ConnectionConfig(f"mock://sync/{index}", "rigol.dg5504pro", "mock"))
    runtime.connect(key).result()
    return key


def test_cli_disabled_atomic_idle_and_control_group(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        key = add(runtime)
        session = runtime.sessions.sessions[key]
        assert not runtime.monitoring.settings_sync.enabled
        assert not runtime.sessions.readback_frames(key)
        gate = threading.Event()
        original = session.adapter.read_settings
        session.adapter.read_settings = lambda names, ch: (gate.wait(3), original(names, ch))[1]
        try:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(
                    pool.map(
                        lambda _: runtime.sessions.try_submit_idle(
                            key, "read_settings", (("output",), 1), quiet=True
                        ),
                        range(8),
                    )
                )
            assert sum(f is not None for f in results) == 1
            assert session.pending == 1
        finally:
            gate.set()
        next(f for f in results if f is not None).result()
        runtime.sessions.set_control_group(key, True)
        assert runtime.sessions.try_submit_idle(key, "read_settings", (("output",), 1)) is None
        runtime.sessions.set_control_group(key, False)


def test_twenty_devices_slow_failure_monitor_and_writes_do_not_collide(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        keys = [add(runtime, i) for i in range(20)]
        lock = threading.Lock()
        active, maximum, history, writes = {}, {}, [], []
        for index, key in enumerate(keys):
            adapter = runtime.sessions.sessions[key].adapter
            original = adapter.read_settings

            def read(names, channel, *, key=key, index=index, original=original):
                with lock:
                    active[key] = active.get(key, 0) + 1
                    maximum[key] = max(maximum.get(key, 0), active[key])
                    history.append((key, channel, names, time.monotonic()))
                try:
                    time.sleep(0.08 if index == 0 else 0.002)
                    if index == 1:
                        raise DeviceError("fixture timeout", "timeout")
                    return original(names, channel)
                finally:
                    with lock:
                        active[key] -= 1

            adapter.read_settings = read
            original_apply = adapter.apply
            adapter.apply = lambda command, key=key, original_apply=original_apply: (
                writes.append(key),
                original_apply(command),
            )[1]
        runtime.sessions.set_control_group(keys[2], True)
        runtime.monitoring.start(
            keys[3], (AcquisitionRequest("status", (1,), quantity="frequency"),), interval=0.05
        )
        runtime.monitoring.focus_settings(keys[0])
        runtime.monitoring.enable_settings_sync()
        try:
            wait_for(
                lambda: all(
                    (4, "offset") in runtime.sessions.readback_frames(k) for k in keys if k not in keys[1:3]
                )
            )
            assert not runtime.sessions.readback_frames(keys[2])
            assert len([h for h in history if h[0] == keys[1]]) == 1  # Degraded sessions never blind-retry.
            assert writes == []
            assert all(count == 1 for count in maximum.values())
            assert all(len(names) <= 4 for _, _, names, _ in history)
            assert runtime.monitoring.settings_sync.max_observed_concurrent <= 4
            assert runtime.monitoring.latest(keys[3], "status", "frequency") is not None
            runtime.sessions.set_control_group(keys[2], False)
            wait_for(lambda: (4, "offset") in runtime.sessions.readback_frames(keys[2]))
            before = runtime.sessions.sessions[keys[0]].configuration_version
            result = runtime.monitoring.apply(
                keys[0], DeviceCommand("frequency", {"value": 1234}, 1)
            ).result()
            assert result.outcome == "success"
            wait_for(
                lambda: (
                    runtime.sessions.readback_frames(keys[0])[(1, "frequency")].configuration_version > before
                )
            )
            assert writes == [keys[0]]
            assert runtime.sessions.readback_frames(keys[0])[(1, "frequency")].reading.value == {
                "value": 1234.0
            }
            assert all(s["pending"] <= 1 for s in runtime.sessions.snapshots())
        finally:
            runtime.sessions.set_control_group(keys[2], False)


def test_intervals_staleness_retry_and_reconnection_versions(tmp_path, monkeypatch):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        key = add(runtime)
        session = runtime.sessions.sessions[key]
        service = SettingsSync(runtime.sessions)  # Driven explicitly by a fake monotonic clock.
        service.enabled = True
        now = [time.monotonic()]
        monkeypatch.setattr("lab_control.application.settings_sync.time.monotonic", lambda: now[0])

        def state():
            return {s["stableDeviceId"]: s for s in runtime.sessions.snapshots()}

        def cycle():
            service.tick(state())
            task = service.tasks[key]
            if task.pending:
                task.pending.result()
            service.tick(state())

        for _ in range(10):
            cycle()
        task = service.tasks[key]
        assert not task.pending
        sequence = session.read_sequence
        now[0] += 9
        cycle()
        assert session.read_sequence == sequence
        service.set_focus(key)
        cycle()
        for _ in range(10):
            cycle()
        assert session.read_sequence > sequence
        runtime.sessions.set_control_group(key, True)
        now[0] += 4
        assert service.status(key, ((1, "output"),))[0] == "同步已暂停"
        runtime.sessions.set_control_group(key, False)
        assert service.status(key, ((1, "output"),))[0] == "待同步"
        old = runtime.sessions.readback_frames(key)[(1, "output")]
        runtime.sessions.disconnect(key)
        runtime.connect(key).result()
        service.tick(state())
        assert service.tasks[key].session_id != old.session_id
        assert not service.current(old, state()[key])
        if service.tasks[key].pending:
            service.tasks[key].pending.result()


def test_recoverable_errors_back_off_and_unchanged_reads_do_not_flood_audit(tmp_path, monkeypatch):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        key = add(runtime)
        service = SettingsSync(runtime.sessions)
        service.enabled = True
        session = runtime.sessions.sessions[key]
        calls = []
        original = session.adapter.read_settings

        def bad(names, channel):
            calls.append((names, channel))
            raise DeviceError("fixture unavailable", "unsupported")

        session.adapter.read_settings = bad
        events = []
        monkeypatch.setattr(runtime.repository, "audit", lambda event, **data: events.append((event, data)))
        clock = [time.monotonic()]
        monkeypatch.setattr("lab_control.application.settings_sync.time.monotonic", lambda: clock[0])
        for delay in (2, 5, 10, 30):
            service.tick({s["stableDeviceId"]: s for s in runtime.sessions.snapshots()})
            task = service.tasks[key]
            with pytest.raises(DeviceError):
                task.pending.result()
            service.tick({s["stableDeviceId"]: s for s in runtime.sessions.snapshots()})
            assert task.retry_at == clock[0] + delay
            count = len(calls)
            for _ in range(10):
                service.tick({s["stableDeviceId"]: s for s in runtime.sessions.snapshots()})
            assert len(calls) == count
            clock[0] += delay
        assert len(events) == 1 and events[0][0] == "settings_sync_state"
        session.adapter.read_settings = original
        for _ in range(15):
            service.tick({s["stableDeviceId"]: s for s in runtime.sessions.snapshots()})
            if service.tasks[key].pending:
                service.tasks[key].pending.result()
        assert not service.tasks[key].errors
        assert len(events) == 2


def test_monitoring_readback_is_reused_by_sync(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        key = add(runtime)
        runtime.sessions.submit(
            key, "observe", AcquisitionRequest("status", (1,), quantity="frequency")
        ).result()
        frame = runtime.sessions.readback_frames(key)[(1, "frequency")]
        calls = []
        adapter = runtime.sessions.sessions[key].adapter
        original = adapter.read_settings
        adapter.read_settings = lambda names, ch: (calls.append((names, ch)), original(names, ch))[1]
        runtime.monitoring.enable_settings_sync()
        wait_for(lambda: (4, "offset") in runtime.sessions.readback_frames(key))
        assert runtime.sessions.readback_frames(key)[(1, "frequency")].sequence == frame.sequence
        assert not any(ch == 1 and "frequency" in names for names, ch in calls)


def test_interrupted_write_invalidates_previous_readback(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        key = add(runtime)
        runtime.sessions.submit(key, "read_settings", (("frequency",), 1)).result()
        old = runtime.sessions.readback_frames(key)[(1, "frequency")]

        def fail(_):
            raise DeviceError("Uncertain write", "timeout")

        runtime.sessions.sessions[key].adapter.apply = fail
        with pytest.raises(DeviceError):
            runtime.monitoring.apply(key, DeviceCommand("frequency", {"value": 1200}, 1)).result()
        assert not SettingsSync.current(old, runtime.sessions.snapshots()[0])


def test_late_error_from_before_a_write_does_not_poison_new_configuration(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        key = add(runtime)
        service = SettingsSync(runtime.sessions)
        service.enabled = True
        adapter = runtime.sessions.sessions[key].adapter
        original = adapter.read_settings

        def fail(*_):
            raise DeviceError("Obsolete read failure", "unsupported")

        adapter.read_settings = fail
        service.tick({s["stableDeviceId"]: s for s in runtime.sessions.snapshots()})
        with pytest.raises(DeviceError):
            service.tasks[key].pending.result()
        adapter.read_settings = original
        runtime.monitoring.apply(key, DeviceCommand("frequency", {"value": 1200}, 1)).result()
        service.tick({s["stableDeviceId"]: s for s in runtime.sessions.snapshots()})
        task = service.tasks[key]
        assert not task.errors and task.failures == 0 and task.retry_at == 0
        if task.pending:
            task.pending.result()


def test_overlapping_shutdown_drains_one_pending_automatic_read(tmp_path):
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    key = add(runtime)
    adapter = runtime.sessions.sessions[key].adapter
    original = adapter.read_settings
    entered, release = threading.Event(), threading.Event()
    closed = []
    original_close = adapter.close
    adapter.close = lambda: (closed.append(True), original_close())[1]

    def slow(names, channel):
        entered.set()
        assert release.wait(3)
        return original(names, channel)

    adapter.read_settings = slow
    runtime.monitoring.enable_settings_sync()
    wait_for(entered.is_set)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(runtime.close) for _ in range(2)]
        try:
            wait_for(lambda: runtime.monitoring.closing)
            assert not runtime.monitoring.settings_sync.enabled
            assert not any(f.done() for f in futures)
        finally:
            release.set()
        for future in futures:
            future.result(timeout=5)
    assert runtime.closed and not runtime.sessions.sessions
    assert closed == [True]


def test_continuous_acquisition_pauses_sync_then_resumes_without_backlog(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        key = add(runtime)
        runtime.sessions.submit(key, "read_settings", (("output",), 1)).result()
        session = runtime.sessions.sessions[key]
        old = session.frames[(1, "output")]
        session.frames[(1, "output")] = replace(old, received_at=time.monotonic() - 4)
        entered, release = threading.Event(), threading.Event()

        def acquire(_):
            entered.set()
            assert release.wait(3)
            return MeasurementBatch(
                session.identity, (Measurement("waveform", "V", [0], quality=Quality.SIMULATED),), {}
            )

        session.adapter.acquire = acquire
        future = runtime.sessions.start_acquisition(key, AcquisitionRequest("waveform", (1,)), count=1)
        runtime.monitoring.focus_settings(key)
        runtime.monitoring.enable_settings_sync()
        try:
            wait_for(entered.is_set)
            wait_for(lambda: key in runtime.monitoring.settings_sync.tasks)
            assert runtime.monitoring.settings_status(key, ((1, "output"),))[0] == "同步已暂停"
            assert not runtime.monitoring.settings_sync.tasks[key].pending
            assert session.read_sequence == old.sequence
        finally:
            release.set()
        future.result()
        wait_for(lambda: runtime.sessions.readback_frames(key)[(1, "output")].sequence > old.sequence)
        assert all(s["pending"] <= 1 for s in runtime.sessions.snapshots())
