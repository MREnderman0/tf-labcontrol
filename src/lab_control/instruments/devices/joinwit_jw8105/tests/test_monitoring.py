"""Display-power protocol through the shared monitoring and recording pipeline."""

import csv
import time

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest
from lab_control.instruments.devices.joinwit_jw8105 import DEFINITION
from lab_control.instruments.devices.joinwit_jw8105.adapter import Jw8105Adapter
from lab_control.instruments.devices.joinwit_jw8105.protocol import build_frame, parse_frame
from lab_control.storage.recording import read_frames


class DisplayPowerTransport:
    resource = "mock://jw8105-display"

    def __init__(self):
        self.history = []

    def query_frame(self, payload):
        frame = parse_frame(payload)
        self.history.append((frame.address, frame.command))
        assert frame.data == b""
        if frame.command == 0x0140:
            return build_frame(frame.address, 0x0141, b"\0" * 4)
        # Reject any hidden wavelength query, standard-power read or setting.
        assert frame.command == 0x0162
        value = -57423 if frame.address == 1 else 1885
        return build_frame(frame.address, 0x0163, value.to_bytes(4, "little", signed=True) * 8)

    def close(self):
        pass


def test_display_power_records_original_precision_without_wavelength_queries(tmp_path):
    transport = DisplayPowerTransport()
    config = ConnectionConfig(transport.resource, "joinwit.jw8105", "mock")
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        runtime.sessions.factory = lambda selected: Jw8105Adapter(transport, DEFINITION.profile, selected)
        device = runtime.add(config)
        runtime.connect(device).result(5)
        runtime.monitoring.enable_settings_sync(True)
        runtime.monitoring.start(
            device,
            (AcquisitionRequest("scalar", channels=(1, 9), points=1, quantity="power"),),
            interval=0.05,
            record_interval=None,
            recording=True,
        )
        task = runtime.monitoring.tasks[device]
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if task.recorder and task.recorder.snapshot()["committed"] >= 2:
                break
            time.sleep(0.01)
        else:
            pytest.fail(f"No committed display-power frames: {runtime.monitoring.snapshots()}")
        runtime.monitoring.stop(device).result(15)
        task.recorder.finish().result(15)
        segments = list(runtime.repository.data_root.glob("**/segment-*.sqlite3"))
        assert len(segments) == 1
        frames = list(read_frames(segments[0]))
        assert len(frames) >= 2
        for frame in frames:
            assert [sample["channel"] for sample in frame["measurements"]] == [1, 9]
            assert [float(sample["values"]) for sample in frame["measurements"]] == pytest.approx(
                [-57.423, 1.885]
            )
            assert all(sample["unit"] == "dBm" for sample in frame["measurements"])
            assert not any("wavelength" in key for key in frame["configuration"])
        csv_paths = list(segments[0].parent.parent.glob("数据_*.csv"))
        assert len(csv_paths) == 1
        with csv_paths[0].open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert len(rows) == len(frames)
        for row in rows:
            assert [float(row["CH1功率(dBm)"]), float(row["CH9功率(dBm)"])] == pytest.approx([-57.423, 1.885])
            assert not any("波长" in key for key in row)
        assert transport.history[0] == (1, 0x0140)
        assert transport.history[1:] == [(1, 0x0162), (2, 0x0162)] * (len(transport.history[1:]) // 2)
