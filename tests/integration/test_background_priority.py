import time

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.settings_sync import SettingsSync
from lab_control.domain.instruments import AcquisitionRequest

from .test_monitoring_timing import connected, wait_for


@pytest.mark.parametrize(
    "model, parameter",
    [
        ("rigol.dsa815", "rbw"),
        ("rigol.dg5504pro", "frequency"),
        ("gwinstek.gpd4303s", "voltage"),
    ],
)
def test_settings_are_low_frequency_but_explicit_refresh_is_immediate(
    tmp_path, monkeypatch, model, parameter
):
    runtime, key = connected(tmp_path)
    with runtime:
        if model != "rigol.dsa815":
            key = runtime.add(ConnectionConfig(f"mock://priority/{model}", model, "mock"))
            runtime.connect(key).result()
        service = SettingsSync(runtime.sessions)
        service.dispatch_interval = 0  # This test isolates the 30/60-second per-device cadence.
        service.enabled = True
        service.set_focus(key)
        clock = [time.monotonic()]
        monkeypatch.setattr("lab_control.application.settings_sync.time.monotonic", lambda: clock[0])

        def cycle():
            service.tick({s["stableDeviceId"]: s for s in runtime.sessions.snapshots()})
            task = service.tasks[key]
            if task.pending:
                task.pending.result(3)

        for _ in range(30):
            cycle()
        session = runtime.sessions.sessions[key]
        sequence = session.read_sequence
        assert service.poll_interval(key) == 30
        clock[0] += 20
        cycle()
        assert session.read_sequence == sequence
        assert service.status(key, ((1, parameter),))[0] == "正常"
        service.request(key)
        cycle()
        assert session.read_sequence > sequence
        service.focus = ""
        assert service.poll_interval(key) == 60


def test_quiet_poll_cannot_claim_idle_gap_in_monitoring_but_user_read_can(tmp_path):
    runtime, key = connected(tmp_path)
    with runtime:
        session = runtime.sessions.sessions[key]
        runtime.sessions.set_monitoring(key, True)
        assert session.pending == 0
        assert runtime.sessions.try_submit_idle(key, "read_settings", (("rbw",), 1), quiet=True) is None
        explicit = runtime.sessions.try_submit_idle(key, "read_settings", (("rbw",), 1))
        assert explicit is not None and explicit.result(3)
        runtime.sessions.set_monitoring(key, False)
        ordinary = runtime.sessions.try_submit_idle(key, "read_settings", (("rbw",), 1), quiet=True)
        assert ordinary is not None and ordinary.result(3)


def test_monitoring_scheduler_never_fills_waiting_interval_with_parameter_polls(tmp_path):
    runtime, key = connected(tmp_path)
    with runtime:
        adapter = runtime.sessions.sessions[key].adapter
        reads = []
        original = adapter.read_settings
        adapter.read_settings = lambda *args: reads.append(args) or original(*args)
        service = runtime.monitoring
        with service.lock:
            service.settings_sync.foreground_interval = 0.01
            service.focus_settings(key)
            service.enable_settings_sync()
            service.start(key, (AcquisitionRequest("spectrum"),), interval=1)
        wait_for(lambda: service.latest(key, "spectrum") is not None)
        time.sleep(0.15)
        service.enable_settings_sync(False)
        service.stop(key).result(3)
        assert reads == []


def test_automatic_batches_are_staggered_and_do_not_catch_up_after_a_pause(tmp_path, monkeypatch):
    runtime, key = connected(tmp_path)
    with runtime:
        keys = [key]
        for index in range(5):
            extra = runtime.add(ConnectionConfig(f"mock://stagger/{index}", "rigol.dsa815", "mock"))
            runtime.connect(extra).result()
            keys.append(extra)
        service = SettingsSync(runtime.sessions)
        service.enabled = True
        clock = [time.monotonic()]
        monkeypatch.setattr("lab_control.application.settings_sync.time.monotonic", lambda: clock[0])
        starts = []
        for current in keys:
            adapter = runtime.sessions.sessions[current].adapter
            original = adapter.read_settings
            adapter.read_settings = lambda *args, current=current, original=original: (
                starts.append((current, clock[0])),
                original(*args),
            )[1]

        def tick():
            service.tick({s["stableDeviceId"]: s for s in runtime.sessions.snapshots()})
            for task in service.tasks.values():
                if task.pending:
                    task.pending.result(3)

        for _ in keys:
            before = len(starts)
            tick()
            assert len(starts) == before + 1
            for _ in range(10):
                tick()
            assert len(starts) == before + 1
            clock[0] += 0.051
        assert {device for device, _ in starts} == set(keys)
        assert all(b[1] - a[1] >= 0.05 for a, b in zip(starts, starts[1:]))
        for current in keys:
            runtime.sessions.set_monitoring(current, True)
        clock[0] += 600
        tick()
        before = len(starts)
        assert before == len(keys)
        for current in keys:
            runtime.sessions.set_monitoring(current, False)
        for _ in range(20):
            tick()
        assert len(starts) == before + 1  # A missed 10 minutes does not create a burst.
