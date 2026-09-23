import socket
import struct
import threading

import numpy as np
import pytest

from lab_control.domain.instruments import DeviceError
from lab_control.transport.base import BufferedReader, block_payload, ieee_block, numeric_values
from lab_control.transport.streams import TcpSocketTransport, VicpTransport
from lab_control.transport.visa import VisaRuntime


def test_fragmentation_concatenation_and_embedded_newline():
    chunks = iter([b"#", b"1", b"4", b"a\nb\x00\n1.25\r\n"])
    reader = BufferedReader(lambda count: next(chunks))
    assert block_payload(reader.message()) == b"a\nb\x00"
    assert reader.message() == b"1.25\r\n"


@pytest.mark.parametrize("raw", [b"", b"#", b"#0abc", b"#x12", b"#212a", b"#11ab", b"#9999999999"])
def test_invalid_blocks(raw):
    with pytest.raises(DeviceError):
        block_payload(raw)


def test_binary_endianness_and_nonfinite():
    for order in (">f4", "<f4"):
        values = np.array([1.25, -3.5], dtype=order)
        np.testing.assert_allclose(numeric_values(ieee_block(values.tobytes()), order), values)
    with pytest.raises(DeviceError, match="Non-finite"):
        numeric_values(b"NaN,1")
    with pytest.raises(DeviceError):
        numeric_values(ieee_block(b"123"))


def test_real_socket_stream_and_vicp_packets():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    errors = []

    def handle():
        try:
            with server.accept()[0] as connection:
                assert connection.recv(128) == b"*IDN?\n"
                for data in (b"#1", b"4A\n", b"BC\n"):
                    connection.sendall(data)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=handle)
    thread.start()
    transport = TcpSocketTransport("127.0.0.1", server.getsockname()[1], timeout=1)
    transport.open()
    assert block_payload(transport.query("*IDN?")) == b"A\nBC"
    transport.close()
    thread.join(2)
    server.close()
    assert not errors
    vicp = VicpTransport("localhost")
    sent = []
    vicp._send = sent.append
    vicp.write("*IDN?")
    assert sent[0] == struct.pack(">BBBBI", 0x81, 1, 1, 0, 6) + b"*IDN?\n"
    chunks = iter(
        [struct.pack(">BBBBI", 0x80, 1, 1, 0, 2), b"AB", struct.pack(">BBBBI", 0x81, 1, 1, 0, 2), b"CD"]
    )
    vicp.reader = BufferedReader(lambda count: next(chunks))
    assert vicp.read() == b"ABCD"


def test_manager_not_closed_during_enumeration_or_live_handle():
    class Handle:
        closed = False

        def close(self):
            self.closed = True

    class Manager:
        closed = False

        def list_resources(self, query):
            return ("USB::one",)

        def open_resource(self, resource, **kwargs):
            return Handle()

        def close(self):
            self.closed = True

    manager = Manager()
    runtime = VisaRuntime(manager=manager)
    handle = runtime.open("USB::one")
    assert runtime.enumerate() == ("USB::one",)
    with pytest.raises(DeviceError):
        runtime.close()
    assert not manager.closed and not handle.closed
    runtime.release(handle)
    runtime.close()
    assert manager.closed and handle.closed
