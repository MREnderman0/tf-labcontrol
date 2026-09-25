import numpy as np
import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError, DeviceIdentity
from lab_control.instruments.registry import build_scpi_adapter
from lab_control.transport.base import MockTransport, ieee_block

from ..adapter import FRAME_SETTINGS, agrees
from ..protocol import boolean, choice, option_table, trace


@pytest.mark.parametrize(
    "transport,resource",
    [
        ("visa", "TCPIP0::192.0.2.1::5555::SOCKET"),
        ("tcp", "192.0.2.1:5555"),
    ],
)
def test_multiline_options_rejected_before_socket_query(transport, resource):
    from dataclasses import replace

    a = adapter()
    a.config = replace(a.config, resource=resource, transport=transport)
    assert not a.capabilities().get("options").readable
    assert a.read_settings(("options",))[0].source == "unavailable"
    with pytest.raises(DeviceError, match="选件表"):
        a.read("options")
    assert a.transport.history == []


def settings(**updates):
    return dict(
        start_frequency=99e6,
        stop_frequency=101e6,
        span=2e6,
        center_frequency=100e6,
        sweep_time=0.1,
        sweep_points=601,
        amplitude_unit="DBM",
        transfer_format="ASC",
        byte_order="NORM",
        **updates,
    )


def adapter(exchanges=()):
    transport = MockTransport(list(exchanges))
    transport.open()
    result = build_scpi_adapter(ConnectionConfig("mock://dsa", "rigol.dsa815"), transport)
    result.identity = DeviceIdentity("RIGOL", "DSA815", "REDACTED")
    return result


@pytest.mark.parametrize("block", [False, True])
def test_ascii_plain_and_definite_block(block):
    raw = b", ".join([b"-1.000000e+02"] * 601)
    measurement = trace(ieee_block(raw) if block else raw, settings())
    assert len(measurement.values) == 601
    assert measurement.unit == "dBm" and measurement.axis_unit == "Hz"
    assert measurement.axis[[0, -1]].tolist() == [99e6, 101e6]
    assert len(measurement.metadata["sha256"]) == 64


@pytest.mark.parametrize("order,dtype", [("NORM", ">f4"), ("SWAP", "<f4")])
def test_real32_endian_and_zero_span(order, dtype):
    config = settings()
    config.update(
        start_frequency=100e6,
        stop_frequency=100e6,
        span=0,
        transfer_format="REAL",
        byte_order=order,
        amplitude_unit="V",
    )
    m = trace(ieee_block(np.arange(601, dtype=dtype).tobytes()), config)
    assert m.values[-1] == 600 and m.unit == "V"
    assert m.axis_unit == "s" and m.axis[-1] == 0.1


@pytest.mark.parametrize(
    "raw", [b"1,2", b"#510000123", b"#11xBAD", b"nan," * 600 + b"1", b"1," * 601, b"inf," * 600 + b"1"]
)
def test_reject_partial_wrong_count_nonfinite(raw):
    with pytest.raises(DeviceError):
        trace(raw, settings())


@pytest.mark.parametrize(
    "op,value,ch",
    [
        ("rbw", 20000, 1),
        ("span", -1, 1),
        ("center_frequency", 1.6e9, 1),
        ("rbw", 10000, 2),
        ("attenuation", 1.5, 1),
        ("reference_level", 21, 1),
    ],
)
def test_invalid_request_has_zero_io(op, value, ch):
    a = adapter()
    with pytest.raises(DeviceError):
        a.apply(DeviceCommand(op, {"value": value}, ch))
    assert not a.transport.history


@pytest.mark.parametrize(
    "exchanges,op,value",
    [
        ([("SENS:BAND:RES:AUTO?", b"1")], "rbw", 10000),
        ([("SENS:FREQ:OFFS?", b"0"), ("SENS:FREQ:SPAN?", b"2000000")], "center_frequency", 1),
        ([("SENS:FREQ:OFFS?", b"0"), ("SENS:FREQ:STOP?", b"100")], "start_frequency", 101),
    ],
)
def test_correlated_invalid_values_have_no_setting_writes(exchanges, op, value):
    a = adapter(exchanges)
    with pytest.raises(DeviceError):
        a.apply(DeviceCommand(op, {"value": value}))
    assert all("?" in command for command in a.transport.history)


def test_timeout_after_write_is_not_retried():
    a = adapter([("SENS:BAND:RES:AUTO OFF", b""), ("SENS:BAND:RES:AUTO?", DeviceError("timeout", "timeout"))])
    result = a.apply(DeviceCommand("rbw_auto", {"value": False}))
    assert result.outcome == "partial"
    assert a.transport.history.count("SENS:BAND:RES:AUTO OFF") == 1


def test_strict_enums_and_numeric_tolerances():
    assert choice(b"BLANK", ("BLAN",)) == "BLAN"
    with pytest.raises(DeviceError):
        boolean(b"unknown")
    assert not agrees("center_frequency", 100e6, 100e6 + 100)
    assert not agrees("rbw", 10000, 10001)
    assert not agrees("continuous", False, 0)


def test_passive_acquisition_changes_no_settings_and_rejects_configuration_change(monkeypatch):
    a = adapter([("TRAC:DATA? TRACE1", b", ".join([b"-100"] * 601))])
    state = dict.fromkeys(FRAME_SETTINGS, 0)
    state.update(settings(), measurement_mode="OFF")
    calls = 0

    def read(op):
        nonlocal calls
        calls += 1
        return state[op] + 1 if calls > len(FRAME_SETTINGS) and op == "span" else state[op]

    monkeypatch.setattr(a, "read", read)
    with pytest.raises(DeviceError, match="configuration changed.*span"):
        a.acquire(AcquisitionRequest("spectrum"))
    assert a.transport.history == ["TRAC:DATA? TRACE1"]


def test_configuration_change_does_not_hide_a_truncated_payload(monkeypatch):
    a = adapter([("TRAC:DATA? TRACE1", b"-100,-101")])
    state = dict.fromkeys(FRAME_SETTINGS, 0)
    state.update(settings(), measurement_mode="OFF")
    calls = 0

    def read(op):
        nonlocal calls
        calls += 1
        return state[op] + 1 if calls > len(FRAME_SETTINGS) and op == "span" else state[op]

    monkeypatch.setattr(a, "read", read)
    with pytest.raises(DeviceError) as exc:
        a.acquire(AcquisitionRequest("spectrum"))
    assert exc.value.code == "parse_error"


def test_wrong_acquisition_channel_rejected_before_io():
    a = adapter()
    with pytest.raises(DeviceError):
        a.acquire(AcquisitionRequest("spectrum", (2,)))
    assert not a.transport.history


def test_options_chinese_panel_and_fixed_points_no_unsupported_query():
    table = "Serial N0.|Option|Option Type|Active\n1|AMK-DSA800|试用版|N"
    assert option_table(table.encode("gbk")) == table
    with pytest.raises(DeviceError):
        option_table(b"options unknown")
    with pytest.raises(DeviceError):
        option_table(b"Serial N0.|Option|Option Type|Active\n")
    a = adapter()
    reading = a.read_settings(("sweep_points",))[0]
    assert reading.value == 601 and reading.source == "model"
    assert a.transport.history == []
    assert a.profile.commands["options"].query == "SYST:OPT?"
    assert a.profile.commands["reference_level"].query == "DISP:WIN:TRAC:Y:RLEV?"


def test_disabled_marker_is_unavailable_without_issuing_invalid_mode_query():
    a = adapter([("CALC:MARK1:STAT?", b"0")])
    reading = a.read_settings(("marker_mode",))[0]
    assert reading.source == "unavailable" and reading.value is None and reading.error
    assert a.transport.history == ["CALC:MARK1:STAT?"]


def test_opening_advanced_page_does_not_execute_write_only_actions():
    a = adapter()
    readings = a.read_settings(("single", "marker_peak"))
    assert all(r.source == "unavailable" for r in readings)
    assert a.transport.history == []


def test_full_resolution_mismatch_not_accepted_as_small_relative_error():
    a = adapter(
        [
            ("SENS:BAND:RES:AUTO?", b"0"),
            ("SENS:DET:FUNC?", b"POS"),
            ("SENS:BAND:RES 10000", b""),
            ("SENS:BAND:RES?", b"10001"),
            ("SYST:ERR?", b'0,"No error"'),
        ]
    )
    result = a.apply(DeviceCommand("rbw", {"value": 10000}))
    assert result.outcome == "rejected" and result.completed_steps == 1
    assert a.transport.history.count("SENS:BAND:RES 10000") == 1


@pytest.mark.parametrize("actual,outcome", [(100000000, "success"), (100010000, "rejected")])
def test_marker_quantization_uses_current_axis_resolution(monkeypatch, actual, outcome):
    a = adapter(
        [
            ("CALC:MARK1:X 100000100", b""),
            ("CALC:MARK1:X?", str(actual).encode()),
            ("SYST:ERR?", b'0,"No error"'),
        ]
    )
    monkeypatch.setattr(a, "preflight", lambda *args: None)
    a.capability_details.update(start_frequency=99e6, stop_frequency=101e6, span=2e6)
    assert a.apply(DeviceCommand("marker_frequency", {"value": 100000100})).outcome == outcome
