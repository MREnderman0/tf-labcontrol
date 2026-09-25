from dataclasses import replace

import pytest

from lab_control.domain.instruments import AcquisitionRequest, DeviceError
from lab_control.transport.diagnostics import capture_io

from .test_grouped_queries import enqueue_group, grouped_adapter
from .test_live_acquisition import state


def stream(run="first"):
    return AcquisitionRequest("spectrum", consistency="stream", context_id=run)


def test_stream_has_one_startup_snapshot_then_only_traces_even_after_one_hour():
    a, values = grouped_adapter(), state()
    enqueue_group(a, values)
    first = a.acquire(stream())
    assert first.validation == "full"
    a._context_at -= 3600
    for _ in range(40):
        a.transport.exchanges.append(("TRAC:DATA? TRACE1", b",".join([b"-99"] * 601)))
        with capture_io() as stats:
            batch = a.acquire(stream())
        assert stats.queries == 1 and batch.validation == "cached"
        assert batch.settings_timestamps == first.settings_timestamps
        assert batch.configuration == first.configuration
        assert batch.measurements[0].axis[0] == values["start_frequency"]
        assert batch.measurements[0].axis[-1] == values["stop_frequency"]
        assert a.acquisition_readings(batch) == ()
        assert batch.performance["acquisition_context_id"] == "first"
    assert len(a.transport.history) == 3 + 40


@pytest.mark.parametrize("boundary", ["new_run", "apply", "error", "reconnect", "monitor", "manual"])
def test_new_runs_and_invalidations_do_not_reuse_a_previous_run_snapshot(boundary):
    a, values = grouped_adapter(), state()
    enqueue_group(a, values)
    a.acquire(stream())
    request = stream()
    if boundary == "new_run":
        request = stream("second")
    elif boundary in {"monitor", "manual"}:
        request = AcquisitionRequest("spectrum", consistency="live" if boundary == "monitor" else "full")
    else:
        a.invalidate_acquisition_context(boundary)
    values.update(start_frequency=109e6, stop_frequency=111e6, center_frequency=110e6)
    enqueue_group(a, values)
    with capture_io() as stats:
        batch = a.acquire(request)
    assert batch.validation == "full" and stats.queries == 3
    assert batch.measurements[0].axis[0] == 109e6


@pytest.mark.parametrize("reply", [b"1,2", b",".join([b"nan"] * 601), DeviceError("timeout", "timeout")])
def test_stream_bad_payload_stops_without_querying_or_retrying(reply):
    a, values = grouped_adapter(), state()
    enqueue_group(a, values)
    a.acquire(stream())
    a.transport.exchanges.append(("TRAC:DATA? TRACE1", reply))
    with capture_io() as stats, pytest.raises(DeviceError):
        a.acquire(stream())
    assert stats.queries == 1 and a._acquisition_context is None
    assert not a.transport.exchanges


def test_stream_requires_explicit_run_context_and_does_not_initiate():
    for request in (replace(stream(), context_id=""), replace(stream(), initiate=True, confirmed=True)):
        a = grouped_adapter()
        with pytest.raises(DeviceError):
            a.acquire(request)
        assert not a.transport.history
