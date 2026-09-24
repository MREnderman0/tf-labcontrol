"""Mixed-device scheduling, including four-channel supplies, without physical I/O."""

import threading
import time

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError


def test_twenty_mixed_devices_reads_are_serial_bounded_fair_and_write_free(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        keys = []
        guard = threading.Lock()
        active, maximum, counts, writes = {}, {}, {}, []
        for index in range(20):
            model = "gwinstek.gpd4303s" if index % 2 == 0 else "tek.afg31252"
            key = runtime.add(ConnectionConfig(f"mock://mixed/{index}", model, "mock"))
            runtime.connect(key).result()
            keys.append(key)
            adapter = runtime.sessions.sessions[key].adapter
            original = adapter.read_settings

            def read(names, channel, *, original=original, index=index, key=key):
                with guard:
                    active[key] = active.get(key, 0) + 1
                    maximum[key] = max(active[key], maximum.get(key, 0))
                    counts[key] = counts.get(key, 0) + 1
                try:
                    assert len(names) <= 4
                    time.sleep(0.08 if index == 0 else 0.002)
                    if index == 2:
                        raise DeviceError("fixture timeout", "timeout")
                    return original(names, channel)
                finally:
                    with guard:
                        active[key] -= 1

            adapter.read_settings = read
            original_apply = adapter.apply
            adapter.apply = lambda command, original=original_apply: (
                writes.append(command),
                original(command),
            )[1]
        runtime.sessions.set_control_group(keys[4], True)
        runtime.monitoring.start(
            keys[6], (AcquisitionRequest("status", (1,), quantity="measured_voltage"),), interval=0.05
        )
        runtime.monitoring.focus_settings(keys[0])
        runtime.monitoring.enable_settings_sync()
        started = time.monotonic()
        deadline = started + 8
        while time.monotonic() < deadline:
            if all(
                (4, "measured_current") in runtime.sessions.readback_frames(key)
                if i % 2 == 0
                else (2, "offset") in runtime.sessions.readback_frames(key)
                for i, key in enumerate(keys)
                if i not in {2, 4}
            ):
                break
            assert all(s["pending"] <= 1 for s in runtime.sessions.snapshots())
            time.sleep(0.01)
        else:
            raise AssertionError("At least one healthy device was starved")
        assert writes == [] and counts[keys[2]] == 1
        assert keys[4] not in counts
        assert all(value == 1 for value in maximum.values())
        assert runtime.monitoring.settings_sync.max_observed_concurrent <= 4
        runtime.sessions.set_control_group(keys[4], False)
        runtime.sessions.submit(keys[4], "apply", DeviceCommand("voltage", {"value": 2.0}, 1)).result()
        assert len(writes) == 1
        runtime.monitoring.refresh_settings(keys[4])
        deadline = time.monotonic() + 5
        while (4, "measured_current") not in runtime.sessions.readback_frames(keys[4]):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert len(writes) == 1
        print(
            {
                "devices": 20,
                "initial_sync_seconds": round(time.monotonic() - started, 3),
                "max_global_auto_reads": runtime.monitoring.settings_sync.max_observed_concurrent,
                "max_per_device": max(maximum.values()),
                "automatic_writes": 0,
            }
        )
