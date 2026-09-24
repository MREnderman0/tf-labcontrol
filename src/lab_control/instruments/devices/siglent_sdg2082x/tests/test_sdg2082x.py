import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.controls import control_capabilities
from lab_control.domain.instruments import DeviceCommand, DeviceError, DeviceIdentity
from lab_control.instruments.devices.siglent_sdg2082x.adapter import (
    Sdg2082xAdapter,
    load_value,
    output_state,
    sdg2082x,
)
from lab_control.instruments.registry import match
from lab_control.transport.base import MockTransport

COUPLING = b"COUP TRACE,OFF,FCOUP,OFF,PCOUP,OFF,ACOUP,OFF"


def wave(ch=1, kind="SINE", frequency=1000, amplitude=0.1, offset=0):
    return f"C{ch}:BSWV WVTP,{kind},FRQ,{frequency}HZ,AMP,{amplitude}V,OFST,{offset}V".encode()


def device(exchanges):
    transport = MockTransport(exchanges)
    transport.open()
    adapter = Sdg2082xAdapter(transport, sdg2082x(), ConnectionConfig("test", "siglent.sdg2082x"))
    return adapter, transport


def test_exact_model_and_available_controls():
    assert match(DeviceIdentity("Siglent Technologies", "SDG2082X")) == "siglent.sdg2082x"
    assert match(DeviceIdentity("SIGLENT", "SDG6032X")) == "siglent.sdg6032x"
    assert match(DeviceIdentity("SIGLENT", "SDG2082X Plus")) == ""
    caps = control_capabilities(ConnectionConfig("test", "siglent.sdg2082x"), 2)
    assert caps.channels == (1, 2)
    assert caps.get("frequency").parameters["value"].maximum == 80e6
    assert caps.get("load").readable and not caps.get("load").writable
    assert not {"upload_waveform", "load_waveform"} & {c.name for c in caps.operations}


@pytest.mark.parametrize("header", ["C2:OUTP ", ""])
def test_output_and_load_parsing(header):
    assert output_state((header + "OFF,LOAD,HZ,PLRT,NOR\r\n").encode()) == "OFF"
    assert load_value((header + "ON,LOAD,HZ,PLRT,NOR").encode()) == "HZ"
    assert load_value((header + "OFF,LOAD,50,PLRT,NOR").encode()) == 50
    with pytest.raises(DeviceError):
        output_state(b"UNKNOWN,LOAD,HZ")
    with pytest.raises(DeviceError):
        load_value(b"OFF,PLRT,NOR")


@pytest.mark.parametrize("ch", [1, 2])
def test_frequency_write_and_readback_are_channel_scoped(ch):
    d, t = device(
        [
            ("COUP?", COUPLING),
            (f"C{ch}:BSWV?", wave(ch)),
            (f"C{ch}:OUTP?", f"C{ch}:OUTP OFF,LOAD,HZ".encode()),
            (f"C{ch}:BSWV FRQ,2000", None),
            (f"C{ch}:BSWV?", wave(ch, frequency=2000)),
        ]
    )
    result = d.apply(DeviceCommand("frequency", {"value": 2000}, channel=ch))
    assert result.outcome == "success" and result.readback == 2000
    assert all(f"C{3 - ch}:" not in str(item) for item in t.history)


@pytest.mark.parametrize("kind,maximum", [("SINE", 80e6), ("SQUARE", 25e6), ("PULSE", 25e6), ("RAMP", 1e6)])
def test_waveform_specific_frequency_limit_prevents_write(kind, maximum):
    exchanges = [] if kind == "SINE" else [("COUP?", COUPLING), ("C1:BSWV?", wave(kind=kind))]
    d, t = device(exchanges)
    with pytest.raises(DeviceError):
        d.apply(DeviceCommand("frequency", {"value": maximum + 1}))
    assert not any("BSWV FRQ," in str(c) for c in t.history)


@pytest.mark.parametrize(
    "load,frequency,amplitude,offset",
    [
        ("50", 1000, 11, 0),
        ("HZ", 30e6, 11, 0),
        ("50", 1000, 8, 2),
        ("HZ", 1000, 0.001, 0),
    ],
)
def test_load_frequency_and_offset_envelope(load, frequency, amplitude, offset):
    d, t = device(
        [
            ("COUP?", COUPLING),
            ("C1:BSWV?", wave(frequency=frequency, offset=offset)),
            ("C1:OUTP?", f"C1:OUTP OFF,LOAD,{load}".encode()),
        ]
    )
    with pytest.raises(DeviceError):
        d.apply(DeviceCommand("amplitude", {"value": amplitude}))
    assert not any("BSWV AMP," in str(c) for c in t.history)


def test_coupling_prevents_cross_channel_changes_but_off_remains_available():
    d, t = device(
        [
            ("COUP?", COUPLING.replace(b"TRACE,OFF", b"TRACE,ON")),
            ("C2:OUTP OFF", None),
            ("C2:OUTP?", b"C2:OUTP OFF,LOAD,HZ"),
        ]
    )
    with pytest.raises(DeviceError):
        d.apply(DeviceCommand("frequency", {"value": 1000}))
    assert d.apply(DeviceCommand("output", {"value": False}, channel=2, confirmed=True)).outcome == "success"
    assert not any("BSWV FRQ," in str(c) for c in t.history)


def test_dc_status_does_not_require_nonexistent_frequency_or_amplitude():
    d, _ = device(
        [
            ("C1:BSWV?", b"C1:BSWV WVTP,DC,OFST,1V"),
            ("C1:OUTP?", b"C1:OUTP OFF,LOAD,HZ"),
            ("C2:BSWV?", wave(2)),
            ("C2:OUTP?", b"C2:OUTP OFF,LOAD,50"),
        ]
    )
    values = d.read_status().values
    assert values["1.offset"] == 1 and "1.frequency" not in values
    assert values["2.frequency"] == 1000 and values["2.load"] == 50


@pytest.mark.parametrize("output", ["ON", "OFF"])
def test_leaving_dc_mode_requires_output_off_without_missing_field_queries(output):
    exchanges = [
        ("COUP?", COUPLING),
        ("C1:BSWV?", b"C1:BSWV WVTP,DC,OFST,1V"),
        ("C1:OUTP?", f"C1:OUTP {output},LOAD,HZ".encode()),
    ]
    if output == "OFF":
        exchanges += [("C1:BSWV WVTP,SINE", None), ("C1:BSWV?", wave())]
    d, t = device(exchanges)
    if output == "ON":
        with pytest.raises(DeviceError):
            d.apply(DeviceCommand("waveform", {"value": "SINE"}))
        assert "C1:BSWV WVTP,SINE" not in t.history
    else:
        assert d.apply(DeviceCommand("waveform", {"value": "SINE"})).outcome == "success"


def test_invalid_channel_and_unconfirmed_output_do_not_send_commands():
    d, t = device([])
    for command in (
        DeviceCommand("frequency", {"value": 1}, channel=3),
        DeviceCommand("output", {"value": True}),
    ):
        with pytest.raises(DeviceError):
            d.apply(command)
    assert t.history == []
