import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import DeviceCommand, DeviceError, DeviceIdentity, ValidationError
from lab_control.instruments.devices.rigol_dg5504pro.protocol import (
    amplitude_envelope,
    catalog,
    trigger_value,
)
from lab_control.instruments.registry import build_scpi_adapter, match
from lab_control.transport.base import MockTransport


def make(exchanges=()):
    transport = MockTransport(list(exchanges))
    transport.open()
    return build_scpi_adapter(ConnectionConfig("mock://dg", "rigol.dg5504pro"), transport), transport


@pytest.mark.parametrize("model", ["DG5504 Pro", "DG5504Pro", "DG5504PRO"])
def test_exact_model(model):
    assert match(DeviceIdentity("RIGOL TECHNOLOGIES", model, "TEST")) == "rigol.dg5504pro"
    assert match(DeviceIdentity("RIGOL", "DG5508 Pro", "TEST")) == ""


@pytest.mark.parametrize(
    "frequency,maximum",
    [(100e6, 20), (100e6 + 1, 10), (250e6, 10), (250e6 + 1, 4), (350e6, 4), (350e6 + 1, 2), (500e6, 2)],
)
def test_voltage_bands(frequency, maximum):
    assert amplitude_envelope(frequency, "SIN", "INF") == (0.002, maximum)
    assert amplitude_envelope(frequency, "SIN", 50) == (0.001, maximum / 2)
    assert amplitude_envelope(frequency, "NOIS", 50) == (0.001, 1)


def test_unknown_load_and_catalog():
    with pytest.raises(ValidationError):
        amplitude_envelope(1000, "SIN", 75)
    assert catalog(b'100,900,"a.arb,ARB,532","b.arb,ARB,1032"') == {"a.arb": 532, "b.arb": 1032}
    with pytest.raises(DeviceError):
        catalog(b"bad reply")


def test_catalog_accepts_folder_without_size_and_unknown_file_kind():
    assert catalog(b'100,900,"folder,FOLD,","fixture.csv,ASC,123","image.png,,456"') == {
        "folder": None,
        "fixture.csv": 123,
        "image.png": 456,
    }
    for raw in (b'100,900,"wave.arb,ARB,"', b'100,900,"one,ARB,1","ONE,ARB,2"'):
        with pytest.raises(DeviceError):
            catalog(raw)


@pytest.mark.parametrize("channel", [1, 2, 3, 4])
def test_channel_encoding_and_trigger_mapping(channel):
    device, _ = make()
    for op, value, expected in [
        ("pulse_width", 0.00025, f"SOUR{channel}:FUNC:PULS:WIDT 0.00025"),
        ("trigger_source", "MAN", f"TRIG{channel}:SOUR BUS"),
        ("pm_deviation", 20, f"SOUR{channel}:PM:DEV 20"),
    ]:
        spec, values = device.validate_command(DeviceCommand(op, {"value": value}, channel, True))
        assert device.encode(op, spec, device._context(channel, values), values) == (expected,)
    assert trigger_value(b"BUS") == "MAN"


@pytest.mark.parametrize(
    "points,flags", [(256, ["END"]), (1024, ["HEAD", "END"]), (1536, ["HEAD", "CONT", "END"])]
)
def test_ascii_chunks(points, flags):
    device, _ = make()
    cmd = DeviceCommand("upload_waveform", {"samples": [0.125] * points, "name": "test_wave"}, 4, True)
    spec, values = device.validate_command(cmd)
    chunks = device.encode(cmd.operation, spec, device._context(4, values), values)
    assert [part.split(",")[1] for part in chunks] == flags
    assert sum(len(part.split(",")) - 2 for part in chunks) == points
    assert all(len(part) < 20000 for part in chunks)


@pytest.mark.parametrize(
    "operation,values,channel",
    [
        ("frequency", {"value": 500e6 + 1}, 1),
        ("phase", {"value": 361}, 1),
        ("ramp_symmetry", {"value": 101}, 1),
        ("upload_waveform", {"samples": [0] * 32, "name": "../user"}, 1),
        ("output", {"value": False}, 5),
    ],
)
def test_invalid_requests_have_no_io(operation, values, channel):
    device, transport = make()
    with pytest.raises(DeviceError):
        device.apply(DeviceCommand(operation, values, channel, True))
    assert not transport.history


def test_off_works_without_coupling_reads():
    device, transport = make([("OUTP4 OFF", b""), ("OUTP4?", b"0"), ("SYST:ERR?", b'0,"No error"')])
    assert device.apply(DeviceCommand("output", {"value": False}, 4, True)).outcome == "success"
    assert len(transport.history) == 3


def test_coupled_other_channel_blocks_without_setting_write():
    entries = []
    for ch in range(1, 4):
        entries += [(f"SOUR{ch}:TRAC?", b"OFF")] + [
            (f"SOUR{ch}:{field}:COUP:STAT?", b"0") for field in ("FREQ", "VOLT", "PHAS")
        ]
    entries += [("SOUR4:TRAC?", b"INV")]
    device, transport = make(entries)
    with pytest.raises(ValidationError):
        device.apply(DeviceCommand("frequency", {"value": 1000}, 1))
    assert all("?" in str(payload) for payload in transport.history)


def test_failed_limit_query_clears_cache_and_does_not_retry():
    device, transport = make([("SOUR2:FREQ? MIN", b"1e-6"), ("SOUR2:FREQ? MAX", b"invalid")])
    device.capability_details["2.frequency.range"] = [0, 500e6]
    with pytest.raises(DeviceError):
        device.bounds("frequency", 2)
    assert "2.frequency.range" not in device.capability_details
    assert len(transport.history) == 2


def test_cleanup_refuses_user_files():
    device, transport = make()
    with pytest.raises(ValidationError):
        device.delete_created_file("user_file", set())
    assert not transport.history


def test_synchronization_reads_each_bundle_member_with_required_channel():
    device, transport = make(
        [
            ("SYNC:BENC?", b"CH1"),
            ("SYNC:BUND? CH1", b"0"),
            ("SYNC:BUND? CH2", b"1"),
            ("SYNC:BUND? CH3", b"0"),
            ("SYNC:BUND? CH4", b"1"),
        ]
    )
    assert device.synchronization() == {
        "benchmark": "CH1",
        "bundle": {"1": False, "2": True, "3": False, "4": True},
    }
    assert len(transport.history) == 5
