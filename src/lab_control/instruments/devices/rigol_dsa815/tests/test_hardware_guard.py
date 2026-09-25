from types import SimpleNamespace

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import DeviceError
from lab_control.transport.base import MockTransport

from ..hardware.state import OTHER_MARKERS, verification_profile
from ..hardware.validate_control import RecordingTransport, TestSession, differences, validate_target


def test_trace_reply_log_contains_only_size_and_hash(tmp_path):
    raw = b"sensitive trace samples are never archived"
    inner = MockTransport([("TRAC:DATA? TRACE1", raw)])
    inner.open()
    path = tmp_path / "wire.jsonl"
    transport = RecordingTransport(inner, path, False)
    assert transport.query("TRAC:DATA? TRACE1") == raw
    log = path.read_text()
    assert "sha256" in log and "bytes" in log and "sensitive trace" not in log
    for forbidden in (
        "*RST",
        "*CLS",
        "MMEM:STOR:TRAC 'backup'",
        "TRAC:DATA TRACE1,1,2,3",
        "SENS:FREQ:CENT 100000000",
    ):
        with pytest.raises(DeviceError):
            transport.write(forbidden)
    assert inner.history == ["TRAC:DATA? TRACE1"]


def test_control_transport_still_forbids_storage_and_binary_write(tmp_path):
    transport = RecordingTransport(MockTransport([]), tmp_path / "wire.jsonl", True)
    for payload in (b"#11x", "MMEM:LOAD:STAT 'x'", "*RST", "SOUR:POW 0", "TRAC:DATA TRACE1,0"):
        with pytest.raises(DeviceError):
            transport.write(payload)


def test_all_four_markers_have_independent_backup_commands():
    profile = verification_profile()
    assert len(OTHER_MARKERS) == 30
    for n in (2, 3, 4):
        assert profile.commands[f"marker{n}_position"].writes == (f"CALC:MARK{n}:X:POS {{value}}",)
        assert not profile.commands[f"marker{n}_frequency"].capability.writable


def test_incomplete_backup_blocks_writes():
    with pytest.raises(DeviceError, match="Incomplete backup"):
        validate_target({}, ConnectionConfig("mock://test", "rigol.dsa815"))


def test_restore_failure_stops_next_write(tmp_path):
    calls = []
    app = SimpleNamespace(sessions=SimpleNamespace(sessions={"test": SimpleNamespace(state="connected")}))
    session = TestSession(app, "test", {}, tmp_path)
    session.snapshot = lambda: {}

    def fail(op, value=None):
        calls.append(op)
        raise DeviceError("timeout", "timeout")

    session.apply = fail
    with pytest.raises(DeviceError, match="timeout"):
        session.restore({})
    assert calls == ["continuous"]


def test_restore_reports_auto_derived_difference_separately_from_mode():
    assert differences({"rbw_auto": True, "rbw": 30000}, {"rbw_auto": True, "rbw": 10000}) == {
        "rbw": {"expected": 10000, "actual": 30000, "tolerance": 0}
    }


def test_recovery_connection_failure_is_not_retried(tmp_path):
    calls = []

    def connect(key, *, configure_acquisition):
        assert not configure_acquisition
        calls.append(key)
        raise DeviceError("identity differs", "identity_conflict")

    sessions = SimpleNamespace(
        sessions={"test": SimpleNamespace(state="degraded")}, disconnect=lambda key: None
    )
    session = TestSession(SimpleNamespace(sessions=sessions, connect=connect), "test", {}, tmp_path)
    with pytest.raises(DeviceError, match="identity"):
        session.restore({})
    assert calls == ["test"]
