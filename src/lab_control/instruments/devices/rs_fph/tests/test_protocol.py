import numpy as np
import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError, DeviceIdentity
from lab_control.instruments.devices.rs_fph.adapter import FRAME_SETTINGS, FphAdapter
from lab_control.instruments.devices.rs_fph.hardware.state import restore
from lab_control.instruments.devices.rs_fph.profile import fph
from lab_control.instruments.devices.rs_fph.protocol import boolean, choice, integer, scalar, trace
from lab_control.transport.base import MockTransport, ieee_block


def context(**changes):
    state = dict(
        start_frequency=99e6,
        stop_frequency=101e6,
        center_frequency=100e6,
        span=2e6,
        sweep_time=0.02,
        amplitude_unit="DBM",
        transfer_format="REAL",
        byte_order="NORM",
        rbw=1e4,
        vbw=1e4,
        rbw_auto=False,
        vbw_auto=False,
        reference_level=-20,
        attenuation=20,
        attenuation_auto=False,
        sweep_time_auto=True,
        detector="APE",
        detector_auto=True,
        trace_mode="WRIT",
        trigger_source="IMM",
        frequency_offset=0,
        reference_offset=0,
        measurement_mode="SAN",
        preamplifier=False,
        sweep_points=711,
        maximum_frequency=2e9,
    )
    return {**state, **changes}


def raw_trace(state=None, values=None):
    state = state or context()
    values = np.linspace(-100, -20, 711) if values is None else np.asarray(values)
    if state["transfer_format"] == "ASC":
        return ",".join(f"{v:.6f}" for v in values).encode() + b"\n"
    return ieee_block(values.astype(">f4" if state["byte_order"] == "NORM" else "<f4").tobytes()) + b"\n"


def device(exchanges):
    transport = MockTransport(exchanges)
    transport.open()
    adapter = FphAdapter(transport, fph(), ConnectionConfig("mock://fph", "rs.fph", timeout=10))
    adapter.identity = DeviceIdentity("Rohde&Schwarz", "FPH", "1321.1111K02/TEST", "V2.71")
    return adapter, transport


def settings_exchanges(state):
    return [
        (fph().commands[op].query, str(int(state[op]) if isinstance(state[op], bool) else state[op]).encode())
        for op in FRAME_SETTINGS
    ]


@pytest.mark.parametrize("fmt,order", [("ASC", "NORM"), ("REAL", "NORM"), ("REAL", "SWAP")])
def test_formats_axis_units_and_exact_points(fmt, order):
    state = context(transfer_format=fmt, byte_order=order)
    result = trace(raw_trace(state), state)
    assert result.values.size == 711 and result.unit == "dBm"
    assert (result.axis[0], result.axis[-1], result.axis_unit) == (99e6, 101e6, "Hz")
    zero = context(start_frequency=100e6, stop_frequency=100e6, span=0, sweep_time=0.1, amplitude_unit="V")
    result = trace(raw_trace(zero), zero)
    assert (result.axis[0], result.axis[-1], result.axis_unit, result.unit) == (0, 0.1, "s", "V")


@pytest.mark.parametrize(
    "payload",
    [
        b"#42844short",
        b"#0data",
        ieee_block(b"x" * 2843),
        raw_trace(values=[1] * 710),
        raw_trace(values=[float("nan")] * 711),
        raw_trace(values=[9.91e37] * 711),
        raw_trace() + b"extra",
    ],
)
def test_reject_malformed_or_unavailable_trace(payload):
    with pytest.raises(DeviceError):
        trace(payload, context())


@pytest.mark.parametrize(
    "change",
    [
        dict(amplitude_unit="DB"),
        dict(span=1),
        dict(center_frequency=1),
        dict(measurement_mode="PMET"),
        dict(frequency_offset=1),
        dict(byte_order="BAD"),
        dict(start_frequency=float("nan")),
    ],
)
def test_reject_inconsistent_context(change):
    with pytest.raises(DeviceError):
        trace(raw_trace(), context(**change))


@pytest.mark.parametrize(
    "decoder,data",
    [
        (boolean, b"2"),
        (integer, b"1.5"),
        (scalar, b"9.91E37"),
        (scalar, b"nan"),
        (lambda b: choice(b, ("ASC", "REAL")), b"UNKNOWN"),
    ],
)
def test_scalar_errors_are_not_fallback_values(decoder, data):
    with pytest.raises(DeviceError):
        decoder(data)


@pytest.mark.parametrize(
    "op,value,channel",
    [
        ("center_frequency", 3e9, 1),
        ("rbw", 20000, 1),
        ("attenuation", 12, 1),
        ("span", -1, 1),
        ("rbw", 10000, 2),
    ],
)
def test_invalid_requests_do_no_io(op, value, channel):
    adapter, transport = device([])
    with pytest.raises(DeviceError):
        adapter.apply(DeviceCommand(op, {"value": value}, channel, True))
    assert transport.history == []


def test_auto_coupling_and_frequency_relation_rejected_before_write():
    adapter, t = device([("INST?", b"SAN"), ("SENS:BAND:RES:AUTO?", b"1")])
    with pytest.raises(DeviceError):
        adapter.apply(DeviceCommand("rbw", {"value": 10000}))
    assert all("?" in p for p in t.history)
    adapter, t = device([("INST?", b"SAN"), ("SENS:FREQ:OFFS?", b"0"), ("SENS:FREQ:SPAN?", b"2000000")])
    with pytest.raises(DeviceError):
        adapter.apply(DeviceCommand("center_frequency", {"value": 100}))
    assert all("?" in p for p in t.history)


def test_no_sweep_points_query_and_no_implicit_marker_activation():
    adapter, t = device([("CALC:MARK1:STAT?", b"0")])
    assert adapter.read("sweep_points") == 711
    reading = adapter.read_settings(("marker_frequency",))[0]
    assert reading.source == "unavailable" and reading.value is None
    assert t.history == ["CALC:MARK1:STAT?"]


def test_binary_preparation_once_preserves_order_and_does_not_fallback():
    adapter, t = device(
        [
            ("FORM?", b"ASC"),
            ("FORM:BORD?", b"SWAP"),
            ("FORM REAL,32", b""),
            ("FORM?", b"REAL"),
            ("FORM:BORD?", b"SWAP"),
            ("SYST:ERR?", b'0,"No error"'),
        ]
    )
    adapter.prepare_acquisition()
    assert t.history.count("FORM REAL,32") == 1
    assert adapter.acquisition_preparation["status"] == "ready"
    adapter, t = device(
        [
            ("FORM?", b"ASC"),
            ("FORM:BORD?", b"NORM"),
            ("FORM REAL,32", b""),
            ("FORM?", DeviceError("timeout", "timeout")),
        ]
    )
    with pytest.raises(DeviceError):
        adapter.prepare_acquisition()
    assert "FORM ASC" not in t.history and t.history.count("FORM REAL,32") == 1


@pytest.mark.parametrize("consistency", ["live", "stream"])
def test_first_full_then_trace_only_preserves_readback_time(consistency):
    state = context()
    exchanges = settings_exchanges(state) + [("TRAC:DATA? TRACE1", raw_trace())] + settings_exchanges(state)
    exchanges += [("TRAC:DATA? TRACE1", raw_trace())]
    adapter, t = device(exchanges)
    request = AcquisitionRequest("spectrum", consistency=consistency, context_id="run1")
    first = adapter.acquire(request)
    before = len(t.history)
    second = adapter.acquire(request)
    assert first.validation == "full" and second.validation == "cached"
    assert len(t.history) - before == 1 and second.performance["logical_queries"] == 1
    assert second.settings_timestamps == first.settings_timestamps
    assert adapter.acquisition_readings(second) == ()
    assert not t.exchanges


def test_invalidated_context_and_new_run_require_full_readback():
    state = context()
    one = settings_exchanges(state) + [("TRAC:DATA? TRACE1", raw_trace())] + settings_exchanges(state)
    adapter, t = device(one * 3)
    adapter.acquire(AcquisitionRequest("spectrum", consistency="live", context_id="a"))
    adapter.invalidate_acquisition_context("manual_refresh")
    assert (
        adapter.acquire(AcquisitionRequest("spectrum", consistency="live", context_id="a")).validation
        == "full"
    )
    assert (
        adapter.acquire(AcquisitionRequest("spectrum", consistency="live", context_id="b")).validation
        == "full"
    )
    assert not t.exchanges


def test_configuration_change_is_not_accepted_or_retried():
    state = context()
    adapter, t = device(
        settings_exchanges(state)
        + [("TRAC:DATA? TRACE1", raw_trace())]
        + settings_exchanges(context(rbw=30000))
    )
    with pytest.raises(DeviceError) as exc:
        adapter.acquire(AcquisitionRequest("spectrum"))
    assert exc.value.code == "configuration_changed" and adapter._acquisition_context is None
    assert t.history.count("TRAC:DATA? TRACE1") == 1


def test_timeout_preserved_and_context_invalidated_without_replay():
    adapter, t = device([("TRAC:DATA? TRACE1", DeviceError("timeout", "timeout"))])
    adapter._context_key = ("live", "a")
    adapter._acquisition_context = context()
    with pytest.raises(DeviceError) as exc:
        adapter.acquire(AcquisitionRequest("spectrum", consistency="live", context_id="a"))
    assert exc.value.code == "timeout" and adapter._acquisition_context is None
    assert t.history == ["TRAC:DATA? TRACE1"]


def test_restore_stops_at_first_failure():
    class Broken:
        def __init__(self):
            self.calls = []

        def apply(self, op, value):
            self.calls.append((op, value))
            raise DeviceError("readback failure", "restoration_failed")

    test = Broken()
    with pytest.raises(DeviceError):
        restore(test, {})
    assert test.calls == [("continuous", False)]


@pytest.mark.parametrize("reported", ["NORM", "SWAP"])
def test_v140_uses_explicit_little_endian_without_decode_retry(reported):
    from dataclasses import replace

    state = context(byte_order=reported)
    raw = raw_trace(context(byte_order="SWAP"))
    adapter, transport = device(
        settings_exchanges(state) + [("TRAC:DATA? TRACE1", raw)] + settings_exchanges(state)
    )
    adapter.identity = replace(adapter.identity, firmware="V1.40")
    batch = adapter.acquire(AcquisitionRequest("spectrum"))
    np.testing.assert_allclose(batch.measurements[0].values, np.linspace(-100, -20, 711), atol=0.00001)
    assert batch.configuration["byte_order"] == reported
    assert batch.configuration["binary_encoding"] == "SWAP"
    assert transport.history.count("TRAC:DATA? TRACE1") == 1


def test_hardware_metadata_preserves_session_error_instead_of_accepting_failed_frame():
    from lab_control.domain.instruments import Measurement, MeasurementBatch, Quality
    from lab_control.instruments.devices.rs_fph.hardware.validate_control import frame_record

    failed = MeasurementBatch(
        DeviceIdentity("Rohde&Schwarz", "FPH"),
        (
            Measurement(
                "spectrum", "", [], quality=Quality.PARSE_ERROR, metadata={"error": "invalid byte order"}
            ),
        ),
        {},
    )
    with pytest.raises(DeviceError, match="invalid byte order") as error:
        frame_record(failed)
    assert error.value.code == "parse_error"


def test_active_delta_marker_rejects_peak_without_writes():
    adapter, transport = device(
        [
            ("INST?", b"SAN"),
            ("CALC:MARK1:STAT?", b"1"),
            ("CALC:MARK:FREQ:MODE?", b"FREQ"),
            ("SENS:FREQ:SPAN?", b"2000000"),
            ("CALC:DELT2:STAT?", b"1"),
        ]
    )
    with pytest.raises(DeviceError, match="delta markers"):
        adapter.apply(DeviceCommand("marker_peak"))
    assert all("?" in command for command in transport.history)


def test_hardware_offscreen_marker_restore_is_not_a_public_capability():
    from lab_control.instruments.devices.rs_fph.hardware.state import (
        OTHER_MARKERS,
        VerificationAdapter,
        verification_profile,
    )

    profile = verification_profile()
    replies = [
        ("INST?", b"SAN"),
        ("CALC:MARK1:STAT?", b"1"),
        ("CALC:MARK:FREQ:MODE?", b"FREQ"),
        ("SENS:FREQ:SPAN?", b"2000000"),
        ("SENS:FREQ:OFFS?", b"0"),
    ]
    replies += [(profile.commands[op].query, b"0") for op in OTHER_MARKERS]
    replies += [
        ("CALC:MARK1:X 200000000 Hz", b""),
        ("CALC:MARK1:X?", b"200000000"),
        ("SYST:ERR?", b'0,"No error"'),
    ]
    transport = MockTransport(replies)
    transport.open()
    adapter = VerificationAdapter(transport, profile, ConnectionConfig("mock://fph", "rs.fph"))
    result = adapter.apply(DeviceCommand("restore_marker_frequency", {"value": 200e6}))
    assert result.outcome == "success"
    assert "restore_marker_frequency" not in fph().commands


def test_zero_span_automatic_sweep_rejected_before_setting_write():
    adapter, transport = device([("INST?", b"SAN"), ("SENS:FREQ:SPAN?", b"0")])
    with pytest.raises(DeviceError, match="manual sweep time"):
        adapter.apply(DeviceCommand("sweep_time_auto", {"value": True}))
    assert transport.history == ["INST?", "SENS:FREQ:SPAN?"]


def test_restore_target_validates_destination_not_current_zero_span():
    from lab_control.instruments.devices.rs_fph.hardware.state import (
        SETTINGS,
        VerificationAdapter,
        validate_target,
        verification_profile,
    )

    profile = verification_profile()
    transport = MockTransport([])
    adapter = VerificationAdapter(transport, profile, ConnectionConfig("mock://restore", "rs.fph"))
    adapter.capability_details.update(span=0, amplitude_unit="DBM")
    target = {op: False for op in SETTINGS}
    target.update({op: value for op, value in context().items() if op in SETTINGS})
    target.update(
        options="0",
        continuous=True,
        sweep_count=1,
        marker_frequency_mode="FREQ",
        marker_enabled=True,
        marker_frequency=100e6,
        marker_search_left=0,
        marker_search_right=2e9,
        display_spacing="LOG",
        display_range=100,
        attenuation_mode="LDIS",
        trigger_slope="POS",
        trace_enabled=True,
        trace2_mode="WRIT",
        detector2="APE",
        detector2_auto=True,
    )
    validate_target(target, adapter)
    assert not transport.history
    assert adapter.capability_details["span"] == 0
