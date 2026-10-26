"""Malformed vendor frames must not reach display or recording through a real session."""

import time

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest
from lab_control.instruments.registry import build_scpi_adapter
from lab_control.storage.recording import read_frames
from lab_control.transport.base import MockTransport


@pytest.mark.parametrize("raw", [b"1 nan", b"2 -100\n1 -110", b"1 -100\ntruncated"])
def test_invalid_frame_is_neither_published_nor_recorded(tmp_path, raw):
    config = ConnectionConfig("mock://53100-fixture", "microchip.53100a", "mock")
    wire = MockTransport(
        [
            ("show version", b"Model: Microchip 53100A\nSoftware: Version 1.78"),
            ("show serial", b"TEST123"),
            ("show state", b"Collecting"),
            ("show state", b"Collecting"),
            ("show inputfreq", b"10 MHz"),
            ("show referencefreq", b"Auto"),
            ("show spectrum", raw),
            ("show state", b"Collecting"),
            ("show inputfreq", b"10 MHz"),
            ("show referencefreq", b"Auto"),
        ]
    )
    wire.open()
    with ApplicationRuntime((config,), root=tmp_path, hardware=False) as runtime:
        runtime.sessions.factory = lambda cfg: build_scpi_adapter(cfg, wire)
        key = runtime.add(config)
        runtime.connect(key).result(timeout=2)
        service = runtime.monitoring
        service.start(
            key, (AcquisitionRequest("spectrum", quantity="phase_noise"),), once=True, recording=True
        )
        deadline = time.monotonic() + 3
        while service.snapshots()[key]["running"] and time.monotonic() < deadline:
            time.sleep(0.01)
        service.stop(key).result(timeout=3)
        assert service.snapshots()[key]["error"]
        assert service.latest(key, "spectrum", "phase_noise") is None
        assert not service.tasks[key].frames and not wire.exchanges
        assert wire.history.count("show spectrum") == 1
        assert not any(
            list(read_frames(path)) for path in runtime.repository.data_root.rglob("segment-*.sqlite3")
        )
        assert not list(runtime.repository.data_root.rglob("*.csv"))
