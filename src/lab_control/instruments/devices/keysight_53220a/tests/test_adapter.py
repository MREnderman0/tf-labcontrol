import math
import struct

import pytest

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError
from lab_control.instruments.registry import build_scpi_adapter

from ..protocol import frequency_block
from .fake import Counter


def adapter():
    wire = Counter()
    result = build_scpi_adapter(
        ConnectionConfig("mock://counter", "keysight.53220a", "mock", expected_serial="TEST-COUNTER"), wire
    )
    result.identify()
    return result, wire


def request(channel=1):
    return AcquisitionRequest(
        kind="scalar", quantity="frequency", channels=(channel,), points=1, initiate=True, confirmed=True
    )


@pytest.mark.parametrize("order,fmt", [("NORM", ">d"), ("SWAP", "<d")])
def test_binary_formats(order, fmt):
    assert frequency_block(b"#18" + struct.pack(fmt, 100e6), order) == 100e6


@pytest.mark.parametrize("value", [math.nan, math.inf, 9.91e37, -9.9e37, 0, -1, 351e6])
def test_invalid_frequency(value):
    with pytest.raises(DeviceError):
        frequency_block(b"#18" + struct.pack(">d", value), "NORM")


@pytest.mark.parametrize("raw", [b"#0abc", b"#18abc", b"#18abcdefghgarbage", b"#216" + b"0" * 16])
def test_bad_blocks(raw):
    with pytest.raises(DeviceError):
        frequency_block(raw, "NORM")


def test_readonly_identification_and_preparation_do_not_write():
    a, wire = adapter()
    a.prepare_acquisition()
    a.snapshot()
    assert all(c.endswith("?") for c in wire.history)


@pytest.mark.parametrize("channels", [(1, 2), (3,)])
def test_channel_rejected_before_io(channels):
    a, wire = adapter()
    before = list(wire.history)
    with pytest.raises(DeviceError):
        a.acquire(
            AcquisitionRequest(
                kind="scalar", quantity="frequency", channels=channels, initiate=True, confirmed=True
            )
        )
    assert wire.history == before


def test_measurement_is_new_and_prepared_once():
    a, wire = adapter()
    original = a.snapshot()
    assert a.acquire(request(2)).measurements[0].values == (100e6,)
    wire.measurement += 1
    assert a.acquire(request(2)).measurements[0].values == (100e6 + 1,)
    assert sum(c.startswith("SENS:FUNC ") for c in wire.history) == 1
    assert wire.history.count("INIT:IMM") == 2
    assert a.restore(original)["settings_match"]


def test_input_constraints_and_channel_isolation():
    a, wire = adapter()
    before = a.snapshot()
    assert a.apply(DeviceCommand("coupling", {"value": "DC"}, 2)).outcome == "success"
    assert wire.state["INP1:COUP"] == before["INP1:COUP"]
    for name, value in [
        ("input_impedance", 75),
        ("input_range", 500),
        ("trigger_level", 0.001),
        ("gate_time", 0),
    ]:
        history = list(wire.history)
        with pytest.raises(DeviceError):
            a.apply(DeviceCommand(name, {"value": value}))
        assert all(c.endswith("?") for c in wire.history[len(history) :])


def test_timeout_is_not_replayed_and_restore_failure_is_visible():
    a, wire = adapter()
    original = a.snapshot()
    wire.failure = "DATA:REM? 1,WAIT"
    with pytest.raises(DeviceError, match="timeout"):
        a.acquire(request())
    assert wire.history.count("INIT:IMM") == 1
    assert wire.history.count("DATA:REM? 1,WAIT") == 1
    wire.failure = "FORM:DATA ASC,15"
    with pytest.raises(DeviceError):
        a.restore(original)


def test_complete_changed_frame_is_not_returned():
    a, wire = adapter()
    wire.during_read = lambda: wire.state.update({"SENS:FREQ:GATE:TIME": "1"})
    with pytest.raises(DeviceError) as error:
        a.acquire(request())
    assert error.value.code == "configuration_changed"


def test_partial_write_and_wrong_serial():
    a, wire = adapter()
    wire.failure = "INP1:COUP DC"
    assert a.apply(DeviceCommand("coupling", {"value": "DC"})).outcome == "uncertain"
    wrong = build_scpi_adapter(
        ConnectionConfig("mock://counter", "keysight.53220a", "mock", expected_serial="DIFFERENT"), Counter()
    )
    with pytest.raises(DeviceError, match="Serial"):
        wrong.identify()


def test_gate_boundaries_manual_level_dependency_and_boolean_readback():
    a, wire = adapter()
    for value in (0.0001, 1000):
        assert a.apply(DeviceCommand("gate_time", {"value": value})).outcome == "success"
    for value in (0.000099, 0.000105, 1000.00001):
        before = len(wire.history)
        with pytest.raises(DeviceError):
            a.apply(DeviceCommand("gate_time", {"value": value}))
        assert not any(not c.endswith("?") for c in wire.history[before:])
    for value in (True, False):
        assert a.apply(DeviceCommand("auto_level", {"value": value})).outcome == "success"
    assert a.apply(DeviceCommand("input_range", {"value": 50})).outcome == "success"
    assert a.apply(DeviceCommand("trigger_level", {"value": 51.25})).outcome == "success"
    with pytest.raises(DeviceError, match="Lower"):
        a.apply(DeviceCommand("input_range", {"value": 5}))


def test_invalid_result_consumes_only_its_reported_timeout_and_can_restore():
    a, wire = adapter()
    original = a.snapshot()
    a.acquire(request())
    wire.measurement = 9.91e37
    wire.error = '+321,"Measurement timeout occurred"'
    with pytest.raises(DeviceError) as exc:
        a.acquire(request(1))
    assert exc.value.code == "overload"
    assert a.restore(original)["settings_match"]


def test_unsupported_format_refused_before_trigger_and_wrong_model_rejected():
    a, wire = adapter()
    a.acquire(request())
    wire.state["FORM:DATA"] = "REAL,32"
    count = wire.history.count("INIT:IMM")
    with pytest.raises(DeviceError) as exc:
        a.acquire(request())
    assert exc.value.code == "not_configured" and wire.history.count("INIT:IMM") == count
    original = wire.query
    wire.query = lambda command: (
        b"Keysight,53230A,TEST-COUNTER,03.00" if command == "*IDN?" else original(command)
    )
    with pytest.raises(DeviceError):
        a.identify()
