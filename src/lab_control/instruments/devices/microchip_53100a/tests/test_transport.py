import socket
import threading

import pytest

from lab_control.domain.instruments import DeviceError
from lab_control.instruments.devices.microchip_53100a.transport import TserveTransport


def serve(exchanges, *, fragments=False):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    commands = []
    errors = []

    def worker():
        try:
            with listener, listener.accept()[0] as client:
                client.settimeout(2)
                client.sendall(b"Welcome\r\n\r\n=127.0.0.1 > ")
                for expected, response in exchanges:
                    data = bytearray()
                    while not data.endswith(b"\r\n"):
                        fragment = client.recv(1)
                        if not fragment:
                            return
                        data.extend(fragment)
                    command = data.decode().strip()
                    commands.append(command)
                    assert command == expected
                    if response is None:
                        client.recv(1)  # Wait for timeout/close without another command.
                    elif fragments:
                        for part in response:
                            client.sendall(part)
                    else:
                        client.sendall(response)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return port, thread, commands, errors


def test_prompt_space_and_empty_set_reply_are_consumed_completely():
    port, thread, commands, errors = serve(
        [
            ("show state", b"Ready\r\nTime Constant: Infinite\r\n=127.0.0.1 > "),
            ("set inputfreq 10", b"=127.0.0.1 > "),
            ("show inputfreq", b"Input Frequency: 10 MHz\r\n\r\n=127.0.0.1 > "),
        ]
    )
    wire = TserveTransport("127.0.0.1", port, 1)
    try:
        wire.open()
        assert wire.query("show state").startswith(b"Ready")
        assert wire.query("set inputfreq 10") == b""
        assert wire.query("show inputfreq") == b"Input Frequency: 10 MHz"
    finally:
        wire.close()
        thread.join(3)
    assert len(commands) == 3 and not errors and not thread.is_alive()


def test_large_spectrum_uses_chunked_reads_and_consumes_the_full_prompt(monkeypatch):
    body = b"Frequency (Hz) Power Spectral Density (dBc/Hz)\r\n" + b"100.0\t-123.456\r\n" * 65_536
    port, thread, commands, errors = serve(
        [
            ("show spectrum", [body, b"=127.0.", b"0.1 >", b" "]),
            ("show state", [b"Collecting\r\n=127.0.0.1 > "]),
        ],
        fragments=True,
    )
    wire = TserveTransport("127.0.0.1", port, 2)
    wire.open()
    receive = wire._receive
    reads = []

    def receive_chunk(count):
        assert count > 1
        reads.append(count)
        return receive(count)

    monkeypatch.setattr(wire, "_receive", receive_chunk)
    monkeypatch.setattr(wire.reader, "receive", receive_chunk)
    try:
        assert wire.query("show spectrum") == body.rstrip()
        assert wire.query("show state") == b"Collecting"
    finally:
        wire.close()
        thread.join(3)
    assert reads and commands == ["show spectrum", "show state"] and not errors


def test_telnet_negotiation_and_literal_ff_can_cross_read_boundaries(monkeypatch):
    wire = TserveTransport("127.0.0.1")
    fragments = iter([b"AB\xff", b"\xfb", b"\x01C\xff", b"\xffD\r", b"\n=127.0.0.1 >", b" "])
    sent = []
    monkeypatch.setattr(wire, "_receive", lambda _: next(fragments))
    monkeypatch.setattr(wire, "_send", sent.append)
    assert wire.read() == b"ABC\xffD"
    assert sent == [b"\xff\xfe\x01"]


def test_timeout_closes_transport_and_never_sends_again():
    port, thread, commands, errors = serve([("set inputfreq 10", None)])
    wire = TserveTransport("127.0.0.1", port, 0.2)
    wire.open()
    with pytest.raises(DeviceError) as caught:
        wire.query("set inputfreq 10")
    assert caught.value.code == "timeout" and wire.sock is None
    with pytest.raises(DeviceError):
        wire.query("show inputfreq")
    thread.join(3)
    assert commands == ["set inputfreq 10"] and not errors
