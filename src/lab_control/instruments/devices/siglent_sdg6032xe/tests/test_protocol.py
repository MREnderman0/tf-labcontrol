import pytest

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import (
    DeviceCommand,
    DeviceError,
    DeviceIdentity,
    Unsupported,
    ValidationError,
)
from lab_control.instruments.registry import build_scpi_adapter, match
from lab_control.transport.base import MockTransport

from ..protocol import basic, output

COUPLING = b"COUP TRACE,OFF,FCOUP,OFF,PCOUP,OFF,ACOUP,OFF,TRDUCH,OFF"


def wave(ch=1, kind="SINE", frequency=1000, amplitude=4, offset=0, phase=0):
    return f"C{ch}:BSWV WVTP,{kind},FRQ,{frequency}HZ,AMP,{amplitude}V,OFST,{offset}V,PHSE,{phase}".encode()


def preflight(ch=1, *, raw=None, load="HZ", state="OFF"):
    return [
        ("COUP?", COUPLING),
        *[(f"C{ch}:{group}?", f"C{ch}:{group} STATE,OFF".encode()) for group in ("MDWV", "SWWV", "BTWV")],
        (f"C{ch}:OUTP?", f"C{ch}:OUTP {state},LOAD,{load},POWERON_STATE,OFF,PLRT,NOR".encode()),
        (f"C{ch}:BSWV?", raw or wave(ch)),
    ]


def device(exchanges):
    wire = MockTransport(exchanges)
    wire.open()
    return build_scpi_adapter(ConnectionConfig("mock://fixture", "siglent.sdg6032xe", "mock"), wire), wire


def test_exact_model_registration_and_old_model_unchanged():
    assert match(DeviceIdentity("Siglent Technologies", "SDG6032X-E")) == "siglent.sdg6032xe"
    assert match(DeviceIdentity("Siglent Technologies", "SDG6032X")) == "siglent.sdg6032x"
    assert match(DeviceIdentity("Siglent Technologies", "SDG6052X-E")) == ""
    assert match(DeviceIdentity("other", "SDG6032X-E")) == ""


@pytest.mark.parametrize("ch", [1, 2])
@pytest.mark.parametrize(
    "name,field,value,changed",
    [
        ("frequency", "FRQ", 2000, {"frequency": 2000}),
        ("amplitude", "AMP", 2, {"amplitude": 2}),
        ("offset", "OFST", 0.1, {"offset": 0.1}),
        ("phase", "PHSE", 30, {"phase": 30}),
    ],
)
def test_basic_apply_targets_selected_channel_and_reads_back(ch, name, field, value, changed):
    adapter, wire = device(
        preflight(ch) + [(f"C{ch}:BSWV {field},{value}", b""), (f"C{ch}:BSWV?", wave(ch, **changed))]
    )
    result = adapter.apply(DeviceCommand(name, {"value": value}, channel=ch))
    assert result.outcome == "success" and result.readback == value
    assert not wire.exchanges
    assert all(f"C{3 - ch}:" not in str(command) for command in wire.history)


@pytest.mark.parametrize("ch", [1, 2])
def test_output_off_has_confirmation_and_bypasses_unrelated_modes(ch):
    adapter, wire = device([(f"C{ch}:OUTP OFF", b""), (f"C{ch}:OUTP?", f"C{ch}:OUTP OFF,LOAD,HZ".encode())])
    with pytest.raises(DeviceError, match="confirmation"):
        adapter.apply(DeviceCommand("output", {"value": False}, channel=ch))
    assert not wire.history
    assert (
        adapter.apply(DeviceCommand("output", {"value": False}, channel=ch, confirmed=True)).outcome
        == "success"
    )


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"C1:BSWV WVTP,SINE,AMP,4V,OFST,0V,PHSE,0",
        wave().replace(b"1000HZ", b"NaNHZ"),
        wave().replace(b"C1:", b"C2:"),
        wave() + b",AMP,4V",
        wave() + b",FRQ",
    ],
)
def test_bad_status_does_not_become_valid_data(raw):
    with pytest.raises(DeviceError):
        basic(raw, 1)


def test_headerless_replies_and_output_extra_fields():
    assert basic(wave(2).split(b" ", 1)[1], 2)["FRQ"] == "1000HZ"
    assert output(b"OFF,LOAD,HZ,POWERON_STATE,OFF,PLRT,NOR", 2) == ("OFF", "HZ")
    with pytest.raises(DeviceError):
        output(b"C1:OUTP MAYBE,LOAD,HZ", 1)


@pytest.mark.parametrize("kind,value", [("SQUARE", 120e6 + 1), ("RAMP", 5e6 + 1)])
def test_waveform_frequency_limits_before_write(kind, value):
    adapter, wire = device(preflight(raw=wave(kind=kind)))
    with pytest.raises(ValidationError):
        adapter.apply(DeviceCommand("frequency", {"value": value}))
    assert all("?" in command for command in wire.history)


@pytest.mark.parametrize(
    "frequency,amplitude,load", [(41e6, 11, "HZ"), (121e6, 6, "HZ"), (161e6, 4, "HZ"), (1000, 11, 50)]
)
def test_amplitude_derating(frequency, amplitude, load):
    adapter, wire = device(preflight(raw=wave(frequency=frequency), load=load))
    with pytest.raises(ValidationError):
        adapter.apply(DeviceCommand("amplitude", {"value": amplitude}))
    assert all("?" in command for command in wire.history)


def test_envelope_coupling_and_readonly_capabilities():
    adapter, wire = device(preflight())
    with pytest.raises(ValidationError):
        adapter.apply(DeviceCommand("offset", {"value": 9}))
    adapter, wire = device([("COUP?", COUPLING.replace(b"FCOUP,OFF", b"FCOUP,ON"))])
    with pytest.raises(ValidationError):
        adapter.apply(DeviceCommand("frequency", {"value": 2000}))
    assert wire.history == ["COUP?"]
    for name in ("modulation", "sweep", "burst", "load", "reference_clock"):
        with pytest.raises(Unsupported):
            adapter.apply(DeviceCommand(name, {"value": True}, confirmed=True))


def test_timeout_after_write_not_replayed_and_not_success():
    adapter, wire = device(
        preflight() + [("C1:BSWV FRQ,2000", b""), ("C1:BSWV?", DeviceError("timeout", "timeout"))]
    )
    result = adapter.apply(DeviceCommand("frequency", {"value": 2000}))
    assert result.outcome == "partial" and result.completed_steps == 1
    assert wire.history.count("C1:BSWV FRQ,2000") == 1


def test_wrong_channel_readback_is_rejected():
    adapter, wire = device(preflight(2) + [("C2:BSWV FRQ,2000", b""), ("C2:BSWV?", wave(1, frequency=2000))])
    assert adapter.apply(DeviceCommand("frequency", {"value": 2000}, channel=2)).outcome == "partial"


def test_invalid_channel_and_range_send_nothing():
    adapter, wire = device([])
    for cmd in (
        DeviceCommand("frequency", {"value": 350e6 + 1}),
        DeviceCommand("phase", {"value": 361}),
        DeviceCommand("frequency", {"value": 1}, channel=3),
    ):
        with pytest.raises(ValidationError):
            adapter.apply(cmd)
    assert not wire.history


def test_dc_status_and_unavailable_phase_are_not_fabricated():
    adapter, wire = device([("C1:BSWV?", b"C1:BSWV WVTP,DC,OFST,1V")])
    reading = adapter.read_settings(("phase",))[0]
    assert reading.source == "unavailable" and reading.value is None
