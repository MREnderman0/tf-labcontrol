import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import DeviceCommand, DeviceError
from lab_control.instruments.registry import build_scpi_adapter

from .state import StateTransport


def device(ch=1, state=None, exchanges=(), limits=None):
    overrides = {
        (f"SOUR{ch}:{key}?" if not key.startswith("OUTP") else key + "?"): str(value).encode()
        for key, value in (state or {}).items()
    }
    transport = StateTransport(exchanges, overrides)
    transport.open()
    adapter = build_scpi_adapter(
        ConnectionConfig("mock://afg", "tek.afg31252", limits=limits or {}), transport
    )
    return adapter, transport


def check(operation, value, state=None, *, valid, ch=1, limits=None):
    adapter, transport = device(ch, state, limits=limits)
    command = DeviceCommand(operation, {"value": value}, ch, True)
    if valid:
        _, values = adapter.validate_command(command)
        adapter.preflight(command, values)
    else:
        with pytest.raises(DeviceError):
            adapter.apply(command)
    assert all(isinstance(q, str) and q.endswith("?") for q in transport.history)
    return transport


@pytest.mark.parametrize("ch", [1, 2])
@pytest.mark.parametrize(
    "waveform,low,high",
    [("SIN", 1e-6, 250e6), ("SQU", 1e-6, 160e6), ("RAMP", 1e-6, 2.5e6), ("EMEM", 0.001, 125e6)],
)
@pytest.mark.parametrize(
    "position,valid", [("low", True), ("high", True), ("below", False), ("above", False)]
)
def test_waveform_specific_frequency(ch, waveform, low, high, position, valid):
    value = {"low": low, "high": high, "below": low / 2, "above": high + 1}[position]
    check("frequency", value, {"FUNC": waveform}, valid=valid, ch=ch)


@pytest.mark.parametrize(
    "waveform,frequency,valid",
    [
        ("SIN", 125e6, True),
        ("SIN", 125e6 + 1, False),
        ("SQU", 160e6, True),
        ("EMEM", 62.5e6, True),
        ("EMEM", 62.5e6 + 1, False),
        ("PRN", 1000, False),
    ],
)
def test_burst_frequency_limits(waveform, frequency, valid):
    check("frequency", frequency, {"FUNC": waveform, "BURS:STAT": 1}, valid=valid)


@pytest.mark.parametrize("waveform", ["PRN", "DC", "UNKNOWN"])
def test_nonperiodic_or_unknown_frequency_rejected(waveform):
    check("frequency", 1000, {"FUNC": waveform}, valid=False)


@pytest.mark.parametrize(
    "load,frequency,amplitude,valid",
    [
        (50, 200e6, 5, True),
        (50, 200e6, 5.001, False),
        (50, 200e6 + 1, 4, True),
        (50, 250e6, 4.001, False),
        (50, 1000, 0.001, True),
        (9.9e37, 1000, 0.001, False),
        (9.9e37, 1000, 0.002, True),
        (9.9e37, 1000, 10, True),
        (9.9e37, 250e6, 8, True),
        (9.9e37, 250e6, 8.01, False),
        (100, 1000, 6, True),
        (100, 1000, 7, False),
        (0, 1000, 0.2, False),
    ],
)
def test_voltage_depends_on_load_and_frequency(load, frequency, amplitude, valid):
    check(
        "amplitude",
        amplitude,
        {"FREQ": frequency, "OUTP1:IMP": load, "VOLT:LIM:HIGH": 5, "VOLT:LIM:LOW": -5},
        valid=valid,
    )


@pytest.mark.parametrize("offset,valid", [(2.4, True), (-2.4, True), (2.4001, False), (-2.4001, False)])
def test_offset_plus_half_amplitude_envelope(offset, valid):
    check("offset", offset, valid=valid)


@pytest.mark.parametrize(
    "operation,value,state",
    [
        ("amplitude", 1, {"VOLT:LIM:HIGH": 0.49}),
        ("offset", -0.2, {"VOLT:LIM:LOW": -0.29}),
        ("frequency", 210e6, {"VOLT": 5}),
        ("waveform", "RAMP", {"FREQ": 3e6}),
        ("amplitude", 0.2, {"VOLT:UNIT": "VRMS"}),
        ("frequency", 2000, {"COMB:FEED": "NOIS"}),
        ("load", 100, {"OUTP1": 1}),
        ("frequency", 2000, {"FSK:STAT": 1}),
        ("frequency", 2000, {"PWM:STAT": 1}),
        ("frequency", 2000, {"FREQ:MODE": "UNKNOWN"}),
        ("frequency", 2000, {"BURS:STAT": 2}),
        ("frequency", 2000, {"FREQ:MODE": "SWE"}),
        ("phase", 0.5, {"FUNC": "PRN"}),
        ("phase", 0.5, {"FUNC": "PULS"}),
        ("pulse_width", 1e-4, {"FUNC": "SIN"}),
        ("ramp_symmetry", 25, {"FUNC": "SIN"}),
    ],
)
def test_correlated_or_unknown_state_blocks_write(operation, value, state):
    check(operation, value, state, valid=False)


@pytest.mark.parametrize("ch", [1, 2])
@pytest.mark.parametrize("coupled_channel", [1, 2])
@pytest.mark.parametrize("group", ["FREQ", "VOLT"])
def test_either_channel_coupling_blocks_independent_write(ch, coupled_channel, group):
    adapter, transport = device(ch)
    transport.state[f"SOUR{coupled_channel}:{group}:CONC?"] = b"1"
    with pytest.raises(DeviceError, match="coupling"):
        adapter.apply(DeviceCommand("frequency", {"value": 2000}, ch))
    assert all(q.endswith("?") for q in transport.history)


@pytest.mark.parametrize(
    "operation,value,state,valid",
    [
        ("pulse_width", 4e-9, {"FREQ": 1e6}, True),
        ("pulse_width", 3e-9, {"FREQ": 1e6}, False),
        ("pulse_width", 0.001, {"FREQ": 1000}, False),
        ("pulse_width", 10e-9, {"PULS:TRAN:LEAD": 10e-9, "PULS:TRAN:TRA": 10e-9}, False),
        ("pulse_duty", 25, {}, True),
        ("pulse_duty", 75, {}, True),
        ("frequency", 160e6, {"PULS:HOLD": "DUTY"}, False),  # A 50% 6.25 ns period violates 4 ns width.
        ("frequency", 0.001, {}, True),
        ("frequency", 0.0009, {}, False),
        ("frequency", 2000, {"PULS:HOLD": "WIDT", "PULS:WIDT": 0.00075}, False),
        ("frequency", 2000, {"PULS:HOLD": "DUTY", "PULS:DCYC": 50}, True),
        ("burst", True, {"PULS:DEL": 0.0006}, False),
        ("frequency", 1000, {"PULS:TRAN:LEAD": 0.001}, False),
    ],
)
def test_pulse_period_edges_hold_and_delay(operation, value, state, valid):
    check(operation, value, {"FUNC": "PULS", **state}, valid=valid)


@pytest.mark.parametrize(
    "operation,value,state,valid",
    [
        ("sweep_stop", 2.5e6, {"FUNC": "RAMP"}, True),
        ("sweep_stop", 2.5e6 + 1, {"FUNC": "RAMP"}, False),
        ("sweep_stop", 999, {}, False),
        ("sweep_start", 2001, {}, False),
        ("sweep_start", 1000, {"FUNC": "PULS"}, False),
        ("sweep_time", 300, {"SWE:HTIM": 100, "SWE:RTIM": 100}, True),
        ("sweep_time", 300, {"SWE:HTIM": 101, "SWE:RTIM": 100}, False),
        ("frequency_mode", "SWE", {"FREQ:STOP": 210e6, "VOLT": 5}, False),
        ("frequency_mode", "SWE", {"AM:STAT": 1}, False),
        ("burst", True, {"PM:STAT": 1}, False),
        ("frequency_mode", "SWE", {"BURS:STAT": 1}, False),
        ("fm_deviation", 999, {}, True),
        ("fm_deviation", 1000, {}, False),
        ("frequency", 50, {"FM:STAT": 1, "FM:DEV": 100}, False),
        ("fm_deviation", 1.250001e6, {"FUNC": "RAMP", "FREQ": 1.25e6}, False),
        ("fm_frequency", 100, {"FM:SOUR": "EXT"}, False),
        ("pm_enabled", True, {"FUNC": "PRN"}, False),
        ("fm_source", "INT", {"FM:DEV": 1e6}, True),  # Inactive deviation need not fit until enabled.
    ],
)
def test_sweep_and_modulation_correlation(operation, value, state, valid):
    check(operation, value, state, valid=valid)


def test_indirect_changes_obey_experiment_limits():
    check("load", 100, limits={"amplitude": 0.21}, valid=False)
    check("frequency_mode", "SWE", limits={"frequency": 1500}, valid=False)
    check("waveform", "SIN", limits={"offset": 0.01}, state={"VOLT:OFFS": 0.02}, valid=False)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1])
def test_invalid_scalar_has_no_io(value):
    assert not check("frequency", value, valid=False).history


def test_live_queries_not_cached_between_commands_and_timeout_not_retried():
    adapter, transport = device()
    command = DeviceCommand("frequency", {"value": 2000})
    _, values = adapter.validate_command(command)
    adapter.preflight(command, values)
    transport.state["SOUR1:FUNC?"] = DeviceError("read timeout", "timeout")
    count = transport.history.count("SOUR1:FUNC?")
    with pytest.raises(DeviceError, match="read timeout"):
        adapter.apply(command)
    assert transport.history.count("SOUR1:FUNC?") == count + 1
    assert all(q.endswith("?") for q in transport.history)


@pytest.mark.parametrize("ch", [1, 2])
@pytest.mark.parametrize("frequency", [1000, 2000])
def test_valid_write_uses_registry_adapter_and_only_target_channel(ch, frequency):
    adapter, transport = device(
        ch,
        exchanges=[
            (f"SOUR{ch}:FREQ {frequency}", b""),
            (f"SOUR{ch}:FREQ?", str(frequency).encode()),
            ("SYST:ERR?", b"0,No error"),
        ],
    )
    result = adapter.apply(DeviceCommand("frequency", {"value": frequency}, ch))
    assert result.outcome == "success" and result.readback == frequency
    assert not transport.exchanges
    assert [q for q in transport.history if not q.endswith("?")] == [f"SOUR{ch}:FREQ {frequency}"]
