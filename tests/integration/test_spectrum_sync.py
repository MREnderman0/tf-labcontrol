import json
import threading
import time

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "scheduler did not settle"
        time.sleep(0.01)


def test_twenty_mixed_devices_cache_fairness_and_serial_io(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        keys, active, maximum, reads, writes = [], {}, {}, [], []
        lock = threading.Lock()
        started = time.monotonic()
        for index in range(20):
            model = "rigol.dsa815" if index < 12 else "tek.afg31252" if index < 16 else "gwinstek.gpd4303s"
            key = runtime.add(ConnectionConfig(f"mock://spectrum-stress/{index}", model, "mock"))
            runtime.connect(key).result()
            keys.append(key)
            adapter = runtime.sessions.sessions[key].adapter

            def wrap(function, operation, key=key, index=index):
                def invoke(*args):
                    with lock:
                        active[key] = active.get(key, 0) + 1
                        maximum[key] = max(maximum.get(key, 0), active[key])
                    try:
                        if operation == "read_settings":
                            reads.append((key, len(args[0])))
                            time.sleep(0.08 if index == 0 else 0.002)
                            if index == 1:
                                raise DeviceError("scripted disconnect", "timeout")
                        elif operation == "apply":
                            writes.append(key)
                        return function(*args)
                    finally:
                        with lock:
                            active[key] -= 1

                return invoke

            for operation in ("read_settings", "apply", "acquire"):
                setattr(adapter, operation, wrap(getattr(adapter, operation), operation))
        runtime.sessions.set_control_group(keys[2], True)
        runtime.monitoring.start(keys[3], (AcquisitionRequest("spectrum", points=601),), interval=0.05)
        runtime.monitoring.focus_settings(keys[0])
        runtime.monitoring.enable_settings_sync()
        wait_for(lambda: all(runtime.sessions.readback_frames(k) for k in keys if k not in keys[1:3]))
        assert not writes
        assert not runtime.sessions.readback_frames(keys[2])
        wait_for(lambda: len(runtime.sessions.readback_frames(keys[3])) == 11)
        runtime.sessions.set_control_group(keys[2], False)
        wait_for(lambda: len(runtime.sessions.readback_frames(keys[2])) == 11)
        assert (
            runtime.monitoring.apply(keys[4], DeviceCommand("reference_level", {"value": -20}))
            .result()
            .outcome
            == "success"
        )
        assert writes == [keys[4]]
        assert all(v == 1 for v in maximum.values())
        assert all(count <= 4 for _, count in reads)
        assert all(s["pending"] <= 1 for s in runtime.sessions.snapshots())
        assert runtime.monitoring.settings_sync.max_observed_concurrent <= 4
        assert len([k for k, _ in reads if k == keys[1]]) == 1
        print(
            "SPECTRUM_METRICS="
            + json.dumps(
                dict(
                    devices=20,
                    spectrum=12,
                    signal_sources=4,
                    power_supplies=4,
                    elapsed_seconds=round(time.monotonic() - started, 3),
                    max_device_io=max(maximum.values()),
                    max_auto_concurrency=runtime.monitoring.settings_sync.max_observed_concurrent,
                    max_batch=max(n for _, n in reads),
                    auto_writes=0,
                    explicit_user_writes=len(writes),
                    fault_device_attempts=len([k for k, _ in reads if k == keys[1]]),
                    all_healthy_devices_served=True,
                )
            )
        )


def test_trace_settings_reuse_and_old_frame_rejected(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        key = runtime.add(ConnectionConfig("mock://cache", "rigol.dsa815", "mock"))
        runtime.connect(key).result()
        session = runtime.sessions.sessions[key]
        calls = []
        original = session.adapter.read_settings
        session.adapter.read_settings = lambda *args: (calls.append(args), original(*args))[1]
        runtime.monitoring.start(key, (AcquisitionRequest("spectrum", points=601),), interval=0.05)
        wait_for(lambda: runtime.monitoring.latest(key, "spectrum") is not None)
        frames = runtime.sessions.readback_frames(key)
        assert len(frames) == 11 and not calls
        runtime.monitoring.stop(key).result()
        runtime.sessions.submit(key, "apply", DeviceCommand("reference_level", {"value": -20})).result()
        assert runtime.monitoring.latest(key, "spectrum") is None
        assert frames[(1, "reference_level")].configuration_version < session.configuration_version
