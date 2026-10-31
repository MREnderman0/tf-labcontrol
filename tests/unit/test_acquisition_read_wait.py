import socket
import threading
from types import SimpleNamespace

import pytest

from lab_control.domain.instruments import DeviceError
from lab_control.transport.read_wait import acquisition_read_wait, read_timeout
from lab_control.transport.streams import SerialTransport, TcpSocketTransport
from lab_control.transport.visa import VisaTransport


@pytest.mark.parametrize(
    "kind,configured,expected", [("serial", 3, 10), ("visa", 3000, 10000), ("visa", 20000, 20000)]
)
def test_only_reads_receive_extended_budget_and_restore_on_failure(kind, configured, expected):
    observed = []
    if kind == "serial":
        transport = SerialTransport("COM99")

        def read(count):
            observed.append(transport.handle.timeout)
            return b"x" * count

        transport.handle = SimpleNamespace(timeout=configured, read=read)
        receive = transport._receive
    else:
        transport = object.__new__(VisaTransport)

        def read(count, **_kwargs):
            observed.append(transport.handle.timeout)
            return b"x" * count

        transport.handle = SimpleNamespace(timeout=configured, read_bytes=read)
        receive = transport._receive
    with acquisition_read_wait(transport=transport):
        assert receive(2) == b"xx"
        assert transport.handle.timeout == expected
    assert transport.handle.timeout == configured
    assert observed == [expected]

    def fail(*_args, **_kwargs):
        observed.append(transport.handle.timeout)
        raise DeviceError("read failed", "timeout")

    if kind == "serial":
        transport.handle.read = fail
    else:
        transport.handle.read_bytes = fail
    with pytest.raises(DeviceError, match="read failed"):
        with acquisition_read_wait(transport=transport):
            receive(2)
    assert transport.handle.timeout == configured and observed[-1] == expected


def test_late_socket_reply_is_received_once_without_reconnect():
    first, peer = socket.socketpair()
    transport = TcpSocketTransport("example.invalid", timeout=0.05)
    transport.sock = first
    first.settimeout(0.05)
    sent = threading.Event()

    def reply():
        assert sent.wait(2)
        threading.Event().wait(0.15)
        peer.sendall(b"valid\n")

    worker = threading.Thread(target=reply)
    worker.start()
    try:
        with acquisition_read_wait(transport=transport):
            sent.set()
            assert transport.read() == b"valid\n"
        assert first.gettimeout() == 0.05
    finally:
        worker.join(2)
        transport.close()
        peer.close()


def test_read_budget_is_worker_local_and_preserves_long_model_deadline():
    observed = []
    with acquisition_read_wait():
        assert read_timeout(3) == 10
        assert read_timeout(120) == 120
        worker = threading.Thread(target=lambda: observed.append(read_timeout(3)))
        worker.start()
        worker.join(2)
    assert read_timeout(3) == 3 and observed == [3]


def test_serial_timeout_is_set_before_send_and_never_between_frame_chunks():
    from lab_control.instruments.devices.joinwit_jw8105.protocol import build_frame
    from lab_control.instruments.devices.joinwit_jw8105.transport import Jw8105SerialTransport

    response = build_frame(1, 0x0163, bytes(32))
    history = []

    class Handle:
        _timeout = 3
        write_timeout = 3
        pending = b""

        @property
        def timeout(self):
            return self._timeout

        @timeout.setter
        def timeout(self, value):
            assert not self.pending, "Must not reconfigure a port with an incoming reply"
            history.append(("timeout", value))
            self._timeout = value

        def write(self, data):
            history.append(("write", self.write_timeout))
            self.pending = response
            return len(data)

        def read(self, count):
            history.append(("read", self.timeout))
            result, self.pending = self.pending[:count], self.pending[count:]
            return result

    transport = Jw8105SerialTransport("COM99", 3)
    transport.handle = Handle()
    with acquisition_read_wait(transport=transport):
        assert transport.query_frame(build_frame(1, 0x0162)) == response
    assert history == [("timeout", 10), ("write", 3), ("read", 10), ("read", 10), ("timeout", 3)]
