import time

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest
from lab_control.instruments.devices.rs_fph.adapter import FphAdapter
from lab_control.instruments.devices.rs_fph.hardware.validate_control import frame_record
from lab_control.instruments.devices.rs_fph.profile import fph

from .test_protocol import context, raw_trace


class StateTransport:
    resource = "mock://fph-session"

    def __init__(self):
        self.state = context(byte_order="SWAP")
        self.history = []

    def open(self):
        pass

    def close(self):
        pass

    def write(self, payload):
        raise AssertionError(f"Unexpected setting {payload}")

    def read(self):
        raise AssertionError("Use query")

    def query(self, payload):
        self.history.append(payload)
        extra = {
            "*IDN?": b"Rohde&Schwarz,FPH,1321.1111K02/TEST,V1.40",
            "*OPT?": b"0",
            "SYST:ERR?": b'0,"No error"',
        }
        if payload in extra:
            return extra[payload]
        if payload == "TRAC:DATA? TRACE1":
            return raw_trace(self.state)
        operation = next(op for op, spec in fph().commands.items() if spec.query == payload)
        value = self.state[operation]
        return str(int(value) if isinstance(value, bool) else value).encode()


def test_factory_session_monitoring_and_metadata_export(tmp_path):
    config = ConnectionConfig("mock://fph-session", "rs.fph", "mock")
    transport = StateTransport()
    with ApplicationRuntime((config,), root=tmp_path, hardware=False) as app:
        app.sessions.factory = lambda _: FphAdapter(transport, fph(), config)
        key = app.add(config)
        app.connect(key, configure_acquisition=False).result(3)
        batch = app.sessions.submit(key, "acquire", AcquisitionRequest("spectrum")).result(3)
        record = frame_record(batch)
        assert record["points"] == 711 and record["axis_unit"] == "Hz"
        app.monitoring.start(key, (AcquisitionRequest("spectrum"),), cadence="fastest", record_interval=None)
        deadline = time.monotonic() + 3
        while app.monitoring.tasks[key].sequence < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        app.monitoring.stop(key).result(3)
        assert app.monitoring.tasks[key].sequence >= 3
        assert not app.monitoring.tasks[key].error
        assert app.monitoring.latest(key, "spectrum").batch.validation == "cached"
        assert not (app.repository.root / "data").exists()


def test_real_adapter_stream_binary_and_csv_preserve_every_frame(tmp_path):
    from pathlib import Path

    from lab_control.storage.recording import read_frames

    config = ConnectionConfig("mock://fph-session", "rs.fph", "mock")
    transport = StateTransport()
    with ApplicationRuntime((config,), root=tmp_path, hardware=False) as app:
        app.sessions.factory = lambda _: FphAdapter(transport, fph(), config)
        key = app.add(config)
        app.connect(key, configure_acquisition=False).result(3)
        transport.history.clear()
        paths = app.sessions.start_acquisition(
            key, AcquisitionRequest("spectrum"), cadence="fastest", count=4, record_interval=None
        ).result(15)
        frames = [batch for path in paths for batch in read_frames(Path(path))]
        assert len(frames) == 4
        assert [b["validation"] for b in frames] == ["full", "cached", "cached", "cached"]
        assert all(len(b["measurements"][0]["values"]) == 711 for b in frames)
        assert all(b["settings_timestamps"] == frames[0]["settings_timestamps"] for b in frames)
        assert transport.history.count("TRAC:DATA? TRACE1") == 4
        assert len(transport.history) == 52
        assert list(tmp_path.rglob("*.csv"))
        assert not app.sessions.snapshots()[0]["controlsLocked"]
