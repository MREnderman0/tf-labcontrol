from types import SimpleNamespace

import pytest

from lab_control.domain.instruments import DeviceError
from lab_control.transport.base import BufferedReader, MockTransport, block_payload
from lab_control.transport.diagnostics import AcquisitionStatistics, capture_io
from lab_control.transport.streams import SerialTransport, TcpSocketTransport
from lab_control.transport.visa import VisaTransport


def test_bulk_text_keeps_following_binary_and_next_text():
    chunks = iter([b"1", b"23\r\n#14A\nBC\n4\n"])
    calls = []

    def receive(count):
        calls.append(count)
        return next(chunks)

    reader = BufferedReader(receive, receive_some=receive)
    assert reader.message() == b"123\r\n"
    assert block_payload(reader.message()) == b"A\nBC"
    assert reader.message() == b"4\n"
    assert calls == [65536, 65536]


def test_visa_text_uses_terminated_read_not_exact_bulk_count():
    seen = []

    class Library:
        def read(self, session, count):
            assert handle.read_termination == "\n"
            seen.append(count)
            return b"1.25\n", 0

    handle = SimpleNamespace(read_termination="\n", visalib=Library(), session=1)
    handle.read_bytes = lambda count, **kwargs: pytest.fail("unnecessary exact read")
    transport = VisaTransport(None, "TCPIP0::192.0.2.1::5555::SOCKET")
    transport.handle = handle
    assert transport.read() == b"1.25\n"
    assert seen == [65536] and handle.read_termination == "\n"


@pytest.mark.parametrize("split", range(1, 12))
def test_bulk_header_preserves_binary_newlines_and_split_crlf(split):
    wire = bytearray(b"\r\n#18A\nB\r\nXYZ\r\n1.25\r\n")
    calls = []

    def receive(count):
        calls.append(count)
        size = min(count, split, len(wire))
        result = bytes(wire[:size])
        del wire[:size]
        return result

    reader = BufferedReader(receive, b"\r\n", receive)
    assert block_payload(reader.message()) == b"A\nB\r\nXYZ"
    assert reader.message() == b"1.25\r\n"
    assert not wire and not reader.buffer


def test_complete_scalar_takes_one_backend_read_and_truncation_never_retries():
    reads = []

    def receive(count):
        reads.append(count)
        return b"1.25\n"

    assert BufferedReader(receive, receive_some=receive).message() == b"1.25\n"
    assert len(reads) == 1
    fault = DeviceError("original timeout", "timeout")
    chunks = iter([b"#14AB", fault])

    def truncated(count):
        value = next(chunks)
        if isinstance(value, Exception):
            raise value
        return value

    with pytest.raises(DeviceError) as caught:
        BufferedReader(truncated, receive_some=truncated).message()
    assert caught.value is fault


def test_tcp_latency_option_and_failed_open_cleanup(monkeypatch):
    import socket

    options = []
    closed = []
    connection = SimpleNamespace(
        settimeout=lambda _: None,
        setsockopt=lambda *args: options.append(args),
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(socket, "create_connection", lambda *args: connection)
    transport = TcpSocketTransport("192.0.2.1")
    transport.open()
    assert options == [(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)]
    transport.close()
    assert closed == [True]

    def fail(*args):
        raise OSError("socket option failed")

    connection.setsockopt = fail
    with pytest.raises(DeviceError, match="socket option failed"):
        transport.open()
    assert closed == [True, True] and transport.sock is None


def test_existing_device_error_code_and_no_retry():
    transport = VisaTransport(None, "TCPIP0::192.0.2.1::5555::SOCKET")
    transport.handle = SimpleNamespace(read_termination="\n")
    fault = DeviceError("bad block", "parse_error")

    def fail():
        raise fault

    transport.reader.message = fail
    with pytest.raises(DeviceError) as caught:
        transport.read()
    assert caught.value is fault


def test_metrics_are_bounded_payload_free_and_query_is_counted_once(monkeypatch):
    monkeypatch.setenv("LAB_CONTROL_IO_DIAGNOSTICS", "1")
    transport = MockTransport([("TRAC:DATA? TRACE1", b"SECRET_WAVEFORM")])
    transport.open()
    with capture_io() as capture:
        assert transport.query("TRAC:DATA? TRACE1") == b"SECRET_WAVEFORM"
    assert capture.queries == capture.reads == capture.writes == 1
    assert capture.received_bytes == 15
    assert "SECRET_WAVEFORM" not in str(capture)
    statistics = AcquisitionStatistics()
    for _ in range(150):
        statistics.add({"acquisition_seconds": 1}, "full")
    assert statistics.snapshot()["frames"] == 120


@pytest.mark.parametrize("kind", ["tcp", "visa", "serial"])
def test_fixed_length_query_accounts_read_once(kind):
    if kind == "tcp":
        transport = TcpSocketTransport("192.0.2.1")
        transport.sock = SimpleNamespace(sendall=lambda _: None)
    elif kind == "visa":
        transport = VisaTransport(None, "ASRL1::INSTR")
        transport.handle = SimpleNamespace(read_termination="\n", write=lambda _: None)
    else:
        transport = SerialTransport("COM1")
        transport.handle = SimpleNamespace(write=len)
    transport.reader.buffer.extend(b"ABC")
    with capture_io() as capture:
        assert transport.query_fixed("READ?", 3) == b"ABC"
    assert capture.queries == capture.writes == capture.reads == 1
    assert capture.received_bytes == 3
