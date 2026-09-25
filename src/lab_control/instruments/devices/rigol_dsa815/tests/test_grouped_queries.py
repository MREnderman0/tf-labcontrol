from dataclasses import replace

import numpy as np
import pytest

from lab_control.domain.instruments import AcquisitionRequest, DeviceError
from lab_control.transport.base import ieee_block
from lab_control.transport.diagnostics import capture_io
from lab_control.transport.scpi import scalar_query_message

from ..adapter import FRAME_SETTINGS
from .test_live_acquisition import enqueue, state
from .test_protocol import adapter


def grouped_adapter():
    a = adapter()
    a.identity = replace(a.identity, firmware="00.01.19.00.02")
    a.config = replace(a.config, resource="TCPIP0::192.0.2.1::inst0::INSTR")
    return a


def group(a, values, fields=FRAME_SETTINGS):
    queried = tuple(op for op in fields if op != "sweep_points")
    query = scalar_query_message(tuple(a.profile.commands[op].query for op in queried))
    reply = b";".join(
        str(int(values[op]) if isinstance(values[op], bool) else values[op]).encode() for op in queried
    )
    return query, reply + b"\n"


def enqueue_group(a, values, fields=FRAME_SETTINGS, raw=None, after=None):
    a.transport.exchanges.extend(
        [
            group(a, values, fields),
            ("TRAC:DATA? TRACE1", raw or b",".join([b"-100"] * 601)),
            group(a, after or values, fields),
        ]
    )


def test_initial_full_has_three_transactions_then_monitor_has_only_trace():
    a, values = grouped_adapter(), state()
    enqueue_group(a, values)
    with capture_io() as io:
        first = a.acquire(AcquisitionRequest("spectrum", consistency="live"))
    assert first.validation == "full" and io.queries == io.reads == io.writes == 3
    assert first.configuration == values
    assert first.performance == {"logical_queries": 45, "configuration_query_mode": "grouped"}
    a.transport.exchanges.append(("TRAC:DATA? TRACE1", b",".join([b"-100"] * 601)))
    with capture_io() as io:
        live = a.acquire(AcquisitionRequest("spectrum", consistency="live"))
    assert live.validation == "cached" and io.queries == 1
    assert live.performance["logical_queries"] == 1
    assert live.settings_timestamps["rbw"] == first.timestamp
    assert not a.transport.exchanges


@pytest.mark.parametrize(
    "resource,firmware",
    [
        ("TCPIP0::192.0.2.1::inst0::INSTR", "unknown"),
        ("TCPIP0::192.0.2.1::5555::SOCKET", "00.01.19.00.02"),
        ("TCPIP0::192.0.2.1::hislip0::INSTR", "00.01.19.00.02"),
        ("USB0::REDACTED::INSTR", "00.01.19.00.02"),
    ],
)
def test_unqualified_firmware_or_transport_never_sends_compound(resource, firmware):
    a, values = grouped_adapter(), state()
    a.config = replace(a.config, resource=resource)
    a.identity = replace(a.identity, firmware=firmware)
    enqueue(a, values)
    with capture_io() as io:
        a.acquire(AcquisitionRequest("spectrum"))
    assert io.queries == 45 and not any(";" in payload for payload in a.transport.history)


@pytest.mark.parametrize("failure", ["missing", "extra", "nan", "boolean", "timeout"])
def test_group_failure_never_retries_or_commits_partial_settings(failure):
    a, values = grouped_adapter(), state()
    query, reply = group(a, values)
    if failure == "missing":
        reply = b"1\n"
    elif failure == "extra":
        reply = reply.rstrip() + b";0\n"
    elif failure == "nan":
        reply = b"NaN;" + reply.split(b";", 1)[1]
    elif failure == "boolean":
        parts = reply.split(b";")
        parts[11] = b"INVALID"
        reply = b";".join(parts)
    else:
        reply = DeviceError("timeout unchanged", "timeout")
    a.transport.exchanges.append((query, reply))
    with pytest.raises(DeviceError) as caught:
        a.acquire(AcquisitionRequest("spectrum"))
    assert caught.value.code == (
        "timeout" if failure == "timeout" else "overload" if failure == "nan" else "parse_error"
    )
    assert a.transport.history == [query] and not a.capability_details and a._acquisition_context is None


@pytest.mark.parametrize("change", ["center_frequency", "amplitude_unit", "transfer_format", "sweep_time"])
def test_change_discards_frame_and_invalidates_context(change):
    a, values = grouped_adapter(), state()
    changed = {**values, change: {"amplitude_unit": "V", "transfer_format": "REAL"}.get(change, 123)}
    enqueue_group(a, values, after=changed)
    with pytest.raises(DeviceError) as caught:
        a.acquire(AcquisitionRequest("spectrum"))
    assert caught.value.code == "configuration_changed" and a._acquisition_context is None
    assert len(a.transport.history) == 3


@pytest.mark.parametrize("order", ["NORM", "SWAP"])
def test_binary_zero_span_and_explicit_full_do_not_use_cached_snapshot(order):
    a, values = grouped_adapter(), state()
    values.update(
        start_frequency=100e6, stop_frequency=100e6, span=0, transfer_format="REAL", byte_order=order
    )
    raw = ieee_block(np.full(601, -100, dtype=">f4" if order == "NORM" else "<f4").tobytes())
    for _ in range(2):
        enqueue_group(a, values, raw=raw)
        result = a.acquire(AcquisitionRequest("spectrum"))
        assert result.validation == "full" and result.measurements[0].axis_unit == "s"
        assert result.measurements[0].axis[-1] == values["sweep_time"]


def test_reconnect_forces_complete_group_and_bad_trace_stays_error():
    a, values = grouped_adapter(), state()
    for invalidate in (False, True):
        if invalidate:
            a.invalidate_acquisition_context("reconnect")
        enqueue_group(a, values)
        assert a.acquire(AcquisitionRequest("spectrum", consistency="live")).validation == "full"
    a.transport.exchanges.append(("TRAC:DATA? TRACE1", b"1,2"))
    with pytest.raises(DeviceError) as caught:
        a.acquire(AcquisitionRequest("spectrum", consistency="live"))
    assert caught.value.code == "parse_error" and a._acquisition_context is None


def test_monitor_does_not_poll_parameters_even_after_an_hour_but_new_run_refreshes():
    a, values = grouped_adapter(), state()
    enqueue_group(a, values)
    request = AcquisitionRequest("spectrum", consistency="live", context_id="monitor1")
    first = a.acquire(request)
    a._context_at -= 3600
    a.transport.exchanges.append(("TRAC:DATA? TRACE1", b",".join([b"-100"] * 601)))
    with capture_io() as io:
        cached = a.acquire(request)
    assert io.queries == 1 and cached.validation == "cached"
    assert cached.settings_timestamps == first.settings_timestamps
    assert not cached.performance["acquisition_context_id"]
    enqueue_group(a, values)
    assert a.acquire(replace(request, context_id="monitor2")).validation == "full"
