from __future__ import annotations

import threading

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError
from lab_control.instruments.devices.joinwit_jw8105 import DEFINITION
from lab_control.instruments.devices.joinwit_jw8105 import transport as jw_transport
from lab_control.instruments.devices.joinwit_jw8105.adapter import Jw8105Adapter
from lab_control.instruments.devices.joinwit_jw8105.protocol import (
    build_frame,
    parse_frame,
    powers_16,
    read_framed,
)


class MockFrameTransport:
    resource = "mock://jw8105"

    def __init__(self, replies: list[bytes]) -> None:
        self.replies = list(replies)
        self.history: list[bytes] = []
        self.opened = True

    def query_frame(self, payload: bytes) -> bytes:
        self.history.append(payload)
        if not self.replies:
            raise AssertionError(f"unexpected command {payload.hex(' ')}")
        return self.replies.pop(0)

    def close(self) -> None:
        self.opened = False


def config(**kwargs) -> ConnectionConfig:
    return ConnectionConfig("COM9", "joinwit.jw8105", "serial", **kwargs)


def test_frame_checksum_and_length_match_protocol_examples() -> None:
    raw = build_frame(1, 0x0140)
    assert raw == bytes.fromhex("7B 01 05 01 40 3E 7D")
    decoded = parse_frame(raw, address=1, command=0x0140)
    assert decoded.data == b""


def test_framed_reader_uses_declared_length_instead_of_tail_search() -> None:
    raw = build_frame(1, 0x0141, b"\x7d\x00\x01\x02")
    pieces = [raw[:3], raw[3:]]

    def read_exact(count: int) -> bytes:
        piece = pieces.pop(0)
        assert len(piece) == count
        return piece

    assert read_framed(read_exact) == raw


def test_identity_uses_connection_reply_without_fabricating_serial() -> None:
    transport = MockFrameTransport([build_frame(1, 0x0141, bytes.fromhex("11 22 33 44"))])
    adapter = Jw8105Adapter(transport, DEFINITION.profile, config())
    identity = adapter.identify()
    assert identity.manufacturer == "Joinwit"
    assert identity.model == "JW8105"
    assert identity.serial == ""
    assert identity.confidence == "model_only"
    assert transport.history == [bytes.fromhex("7B 01 05 01 40 3E 7D")]


def test_power_read_decodes_eight_signed_values() -> None:
    payload = bytes.fromhex("60 E3 D3 E2 B4 E2 FE E2 B4 E2 B4 E2 F5 E2 F1 E2")
    transport = MockFrameTransport([build_frame(1, 0x0143, payload)])
    adapter = Jw8105Adapter(transport, DEFINITION.profile, config())
    assert adapter.read("power", 1) == pytest.approx(-73.28)
    assert powers_16(payload)[7] == pytest.approx(-74.39)
    assert transport.history == [bytes.fromhex("7B 01 05 01 42 3C 7D")]


def test_sixteen_channel_acquisition_reads_both_fixed_eight_channel_modules() -> None:
    module_one = b"".join(int(index - 73281).to_bytes(4, "little", signed=True) for index in range(8))
    module_two = b"".join(int(index + 1885).to_bytes(4, "little", signed=True) for index in range(8))
    transport = MockFrameTransport(
        [
            build_frame(1, 0x0141, b"\x11\x22\x33\x44"),
            build_frame(1, 0x0163, module_one),
            build_frame(2, 0x0163, module_two),
        ]
    )
    adapter = Jw8105Adapter(transport, DEFINITION.profile, config())
    adapter.identify()
    batch = adapter.acquire(AcquisitionRequest(channels=tuple(range(1, 17))))
    assert [measurement.channel for measurement in batch.measurements] == list(range(1, 17))
    assert [measurement.values for measurement in batch.measurements] == pytest.approx(
        [(index - 73281) / 1000 for index in range(8)] + [(index + 1885) / 1000 for index in range(8)]
    )
    assert all(measurement.unit == "dBm" for measurement in batch.measurements)
    assert transport.history == [build_frame(1, 0x0140), build_frame(1, 0x0162), build_frame(2, 0x0162)]


def test_read_settings_and_acquisition_are_read_only() -> None:
    # Complete display-power example from V23.02.02, page 5; signed 32-bit / 1000.
    power = bytes.fromhex(
        "5D 07 00 00 B1 1F FF FF D1 3C FF FF E2 3B FF FF 55 3E FF FF A7 3B FF FF 60 3D FF FF 5E 3C FF FF"
    )
    replies = [build_frame(1, 0x0141, b"\x11\x22\x33\x44"), build_frame(1, 0x0163, power)]
    transport = MockFrameTransport(replies)
    adapter = Jw8105Adapter(transport, DEFINITION.profile, config())
    adapter.identify()
    batch = adapter.acquire(AcquisitionRequest(channels=(1, 2)))
    assert [item.values for item in batch.measurements] == pytest.approx([1.885, -57.423])
    assert transport.history == [build_frame(1, 0x0140), build_frame(1, 0x0162)]


@pytest.mark.parametrize("options, channels", [((), (9, 16)), (("module=2",), (1, 8))])
def test_display_acquisition_only_queries_selected_module(options, channels) -> None:
    power = b"".join(int(index - 1000).to_bytes(4, "little", signed=True) for index in range(8))
    identity_module = 2 if options else 1
    transport = MockFrameTransport(
        [build_frame(identity_module, 0x0141, b"\0" * 4), build_frame(2, 0x0163, power)]
    )
    adapter = Jw8105Adapter(transport, DEFINITION.profile, config(options=options))
    adapter.identify()
    batch = adapter.acquire(AcquisitionRequest(channels=channels))
    assert [item.channel for item in batch.measurements] == list(channels)
    assert [item.values for item in batch.measurements] == pytest.approx([-1, -0.993])
    assert transport.history == [build_frame(identity_module, 0x0140), build_frame(2, 0x0162)]


@pytest.mark.parametrize(
    "reply",
    [build_frame(2, 0x0163, b"\0" * 16), build_frame(2, 0x0143, b"\0" * 32)],
)
def test_invalid_second_module_display_reply_rejects_whole_acquisition(reply) -> None:
    transport = MockFrameTransport(
        [build_frame(1, 0x0141, b"\0" * 4), build_frame(1, 0x0163, b"\0" * 32), reply]
    )
    adapter = Jw8105Adapter(transport, DEFINITION.profile, config())
    adapter.identify()
    with pytest.raises(DeviceError) as error:
        adapter.acquire(AcquisitionRequest(channels=(1, 9)))
    assert error.value.code == "parse_error"
    assert transport.history == [build_frame(1, 0x0140), build_frame(1, 0x0162), build_frame(2, 0x0162)]


def test_writes_require_confirmation_and_read_back() -> None:
    transport = MockFrameTransport(
        [
            build_frame(1, 0x0147),
            build_frame(1, 0x0149, bytes.fromhex("B8 FF 01 00")),
        ]
    )
    adapter = Jw8105Adapter(transport, DEFINITION.profile, config())
    rejected = adapter.apply(DeviceCommand("wavelength", {"value": 1310.0}))
    assert rejected.outcome == "uncertain"
    assert rejected.error
    assert transport.history == []

    result = adapter.apply(DeviceCommand("wavelength", {"value": 1310.0}, confirmed=True))
    assert result.outcome == "success"
    assert result.readback == pytest.approx(1310.0)
    assert transport.history == [
        bytes.fromhex("7B 01 09 01 46 B8 FF 01 00 7C 7D"),
        bytes.fromhex("7B 01 05 01 48 36 7D"),
    ]


def test_bad_checksum_is_rejected_before_decoding() -> None:
    raw = bytearray(build_frame(1, 0x0141, b"\x00\x00\x00\x00"))
    raw[-2] ^= 1
    with pytest.raises(DeviceError, match="checksum"):
        parse_frame(bytes(raw))


def test_capabilities_are_registered_but_model_only_identity_has_no_serial() -> None:
    caps = DEFINITION.capability_schema(config())
    assert caps.channels == tuple(range(1, 17))
    assert {item.name for item in caps.operations} == {
        "power",
        "display_power",
        "wavelength",
        "sampling_rate",
        "display_wavelengths",
        "pdl",
    }
    assert DEFINITION.profile.revision.startswith("V23.02.02")


def test_single_module_configuration_keeps_eight_channel_layout() -> None:
    assert DEFINITION.capability_schema(config(options=("channels=8",))).channels == tuple(range(1, 9))
    assert DEFINITION.capability_schema(config(options=("module=2",))).channels == tuple(range(1, 9))
    with pytest.raises(Exception, match="channels=8"):
        DEFINITION.capability_schema(config(options=("module=1", "channels=16")))


def test_invalid_module_option_is_rejected() -> None:
    with pytest.raises(Exception, match="module"):
        Jw8105Adapter(MockFrameTransport([]), DEFINITION.profile, config(options=("module=3",)))


def test_serial_frame_timeout_closes_poisoned_handle(monkeypatch) -> None:
    transport = object.__new__(jw_transport.Jw8105SerialTransport)
    transport.lock = threading.RLock()
    closed: list[bool] = []
    transport.write = lambda _payload: None
    transport._read_exact = lambda _count: b""
    transport.close = lambda: closed.append(True)

    def timed_out(_reader):
        raise DeviceError("Serial read timeout", "timeout")

    monkeypatch.setattr(jw_transport, "read_framed", timed_out)
    with pytest.raises(DeviceError, match="timeout"):
        transport.query_frame(b"request")
    assert closed == [True]


def test_visa_frame_timeout_is_normalized_and_closes_handle(monkeypatch):
    from pyvisa.constants import StatusCode
    from pyvisa.errors import VisaIOError

    transport = object.__new__(jw_transport.Jw8105VisaTransport)
    transport.lock = threading.RLock()
    writes, closed = [], []
    transport.write = writes.append
    transport._read_exact = lambda _count: b""
    transport.close = lambda: closed.append(True)

    def timed_out(_reader):
        raise VisaIOError(StatusCode.error_timeout)

    monkeypatch.setattr(jw_transport, "read_framed", timed_out)
    with pytest.raises(DeviceError) as failure:
        transport.query_frame(build_frame(1, 0x0162))
    assert failure.value.code == "timeout"
    assert closed == [True] and writes == [build_frame(1, 0x0162)]
