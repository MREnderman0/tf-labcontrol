"""Direct recording preserves the same device error boundary as single reads."""

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.leases import ResourceLeases
from lab_control.application.sessions import SessionManager
from lab_control.demo.instrument import SimulatedInstrument
from lab_control.domain.instruments import AcquisitionRequest, DeviceError
from lab_control.storage.repository import Repository


@pytest.mark.parametrize(
    ("code", "expected_state", "expected_version"),
    [("configuration_changed", "connected", 1), ("timeout", "degraded", 0), ("parse_error", "degraded", 0)],
)
def test_direct_failure_classification_and_device_isolation(
    tmp_path, monkeypatch, code, expected_state, expected_version
):
    repository = Repository(tmp_path)
    sessions = SessionManager(ResourceLeases(), repository, SimulatedInstrument)
    calls = []
    error = DeviceError(
        "complete reply configuration changed" if code == "configuration_changed" else code, code
    )
    try:
        for device_id in ("target", "other"):
            sessions.connect(
                device_id, ConnectionConfig(f"mock://{device_id}", "rigol.dg5504pro", "mock")
            ).result(3)
        session = sessions.sessions["target"]

        def fail(request):
            calls.append(request)
            raise error

        monkeypatch.setattr(session.adapter, "acquire", fail)
        future = sessions.start_acquisition("target", AcquisitionRequest("waveform", points=32), count=1)
        peer = sessions.submit("other", "acquire", AcquisitionRequest("waveform", points=32))
        with pytest.raises(DeviceError) as failure:
            future.result(5)
        assert failure.value is error
        assert len(calls) == 1  # The direct API ends on this frame; no automatic retry or replay.
        assert future.done() and not future.cancelled()
        assert session.pending == session.control_pending == 0
        assert not session.controls_locked
        assert session.direct_recording.binary_done
        assert session.direct_recording.finished.done()
        assert session.direct_recording.received == session.direct_recording.committed == 0
        assert session.state == expected_state
        assert session.configuration_version == expected_version
        assert peer.result(3).identity == sessions.sessions["other"].identity
        assert sessions.sessions["other"].configuration_version == 0
        assert sessions.sessions["other"].state == "connected"
        if code == "configuration_changed":
            sessions.submit("target", "status").result(3)
        else:
            with pytest.raises(DeviceError) as rejected:
                sessions.submit("target", "status")
            assert rejected.value.code == "disconnected"
        sessions.close()
        assert not sessions.sessions
        for device_id in ("target", "other"):
            assert sessions.leases.owner(f"mock://{device_id}") is None
    finally:
        sessions.close()
        repository.close()
