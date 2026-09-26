from types import SimpleNamespace

import pytest
from pyvisa.constants import StatusCode

from lab_control.domain.instruments import DeviceError
from lab_control.transport.base import block_payload
from lab_control.transport.visa import VisaTransport


def transport(raw):
    history, counts = [], []
    pending = bytearray(raw)

    def read(session, count):
        counts.append(count)
        result = bytes(pending[:count])
        del pending[:count]
        return result, StatusCode.success_max_count_read if pending else StatusCode.success

    handle = SimpleNamespace(
        read_termination="\n", session=1, visalib=SimpleNamespace(read=read), write=history.append
    )
    runtime = SimpleNamespace(release=lambda h: history.append("closed"))
    wire = VisaTransport(runtime, "TCPIP0::example::inst0::INSTR")
    wire.handle = handle
    return wire, history, counts


def test_native_end_and_embedded_newlines_are_preserved():
    raw = b"C1:WF ALL,#3100" + b"\x00\n" * 50 + b"\n"
    wire, history, counts = transport(raw)
    assert wire.query_binary("C1:WF? ALL", 200) == raw
    assert history == ["C1:WF? ALL"] and counts[0] == 64
    assert wire.handle.read_termination == "\n"


@pytest.mark.parametrize(
    "raw,message",
    [
        (b"C1:WF ALL,#9128000346" + b"x" * 100, "exceeds"),
        (b"#3100xx", "Truncated"),
        (b"#11xjunk", "Trailing"),
    ],
)
def test_abort_closes_connection_records_lengths_and_never_reuses(raw, message):
    wire, history, counts = transport(raw)
    with pytest.raises(DeviceError, match=message) as exc:
        wire.query_binary("WF?", 200)
    assert "declared=" in str(exc.value) and "received=" in str(exc.value)
    assert wire.handle is None and history == ["WF?", "closed"]
    with pytest.raises(DeviceError, match="reconnect"):
        wire.query_binary("WF?", 200)
    assert len(counts) == 1


@pytest.mark.parametrize(
    "raw,message", [(b"#9100000000", "exceeds"), (b"#15xx", "Truncated"), (b"#11xy", "Trailing")]
)
def test_complete_block_distinguishes_failure_lengths(raw, message):
    with pytest.raises(DeviceError, match=message) as exc:
        block_payload(raw)
    assert "declared=" in str(exc.value)


def test_temporary_completion_timeout_restored_and_failure_closes(monkeypatch):
    wire, history, _ = transport(b"")
    wire.handle.timeout = 3000
    observed = []
    monkeypatch.setattr(wire, "query", lambda payload: observed.append(wire.handle.timeout) or b"*OPC 1\n")
    assert wire.query_timeout("*OPC?", 30) == b"*OPC 1\n"
    assert observed == [30000] and wire.handle.timeout == 3000

    def failure(payload):
        raise DeviceError("timeout", "timeout")

    monkeypatch.setattr(wire, "query", failure)
    with pytest.raises(DeviceError):
        wire.query_timeout("*OPC?", 30)
    assert wire.handle is None and history == ["closed"]
