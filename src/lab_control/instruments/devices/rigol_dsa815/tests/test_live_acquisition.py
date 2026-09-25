from dataclasses import replace

import pytest

from lab_control.domain.instruments import AcquisitionRequest, DeviceError
from lab_control.transport.diagnostics import capture_io

from ..adapter import FRAME_SETTINGS
from .test_protocol import adapter, settings


def state():
    return {
        **settings(),
        "rbw": 10000,
        "vbw": 3000,
        "rbw_auto": False,
        "vbw_auto": False,
        "reference_level": -20,
        "attenuation": 20,
        "attenuation_auto": False,
        "sweep_time_auto": False,
        "detector": "NORM",
        "trace_mode": "WRIT",
        "trigger_source": "IMM",
        "frequency_offset": 0,
        "reference_offset": 0,
        "measurement_mode": "OFF",
    }


def enqueue(a, values, fields=FRAME_SETTINGS, raw=None):
    reads = [
        (
            a.profile.commands[op].query,
            str(int(values[op]) if isinstance(values[op], bool) else values[op]).encode(),
        )
        for op in fields
        if op != "sweep_points"
    ]
    a.transport.exchanges.extend([*reads, ("TRAC:DATA? TRACE1", raw or b",".join([b"-100"] * 601)), *reads])


def test_full_live_query_budget_and_original_metadata_times(monkeypatch):
    a, values = adapter(), state()
    enqueue(a, values)
    with capture_io() as stats:
        first = a.acquire(AcquisitionRequest("spectrum", consistency="live"))
    assert first.validation == "full" and stats.queries == 45
    assert stats.writes == stats.reads == 45
    a.transport.exchanges.append(("TRAC:DATA? TRACE1", b",".join([b"-100"] * 601)))
    monkeypatch.setattr("lab_control.instruments.devices.rigol_dsa815.adapter.utc_now", lambda: "new-time")
    with capture_io() as stats:
        fast = a.acquire(AcquisitionRequest("spectrum", consistency="live"))
    assert fast.validation == "cached" and stats.queries == 1
    assert fast.settings_timestamps["rbw"] == first.timestamp
    assert fast.settings_timestamps["span"] == first.timestamp
    assert not a.acquisition_readings(fast)
    assert not a.transport.exchanges


@pytest.mark.parametrize("reason", ["explicit_read", "apply", "reconnect", "error"])
def test_context_invalidation_requires_full_read(reason):
    a, values = adapter(), state()
    enqueue(a, values)
    a.acquire(AcquisitionRequest("spectrum"))
    a.invalidate_acquisition_context(reason)
    enqueue(a, values)
    assert a.acquire(AcquisitionRequest("spectrum", consistency="live")).validation == "full"


@pytest.mark.parametrize("change", ["axis", "unit", "format", "zero_span"])
def test_explicit_refresh_after_front_panel_changes_publishes_new_axis(change):
    a, values = adapter(), state()
    enqueue(a, values)
    a.acquire(AcquisitionRequest("spectrum"))
    if change == "axis":
        values.update(center_frequency=110e6, start_frequency=109e6, stop_frequency=111e6)
    elif change == "unit":
        values["amplitude_unit"] = "V"
    elif change == "format":
        values["byte_order"] = "SWAP"
    else:
        values.update(start_frequency=100e6, stop_frequency=100e6, span=0)
    a.invalidate_acquisition_context("explicit_read")
    enqueue(a, values)
    result = a.acquire(AcquisitionRequest("spectrum", consistency="live"))
    assert result.validation == "full" and result.configuration == values


def test_explicit_full_never_uses_live_cache_and_bad_payload_is_not_a_transition():
    a, values = adapter(), state()
    request = AcquisitionRequest("spectrum")
    enqueue(a, values)
    a.acquire(request)
    enqueue(a, values)
    with capture_io() as stats:
        assert a.acquire(request).validation == "full"
    assert stats.queries == 45
    values["span"] = 1e6
    a.transport.exchanges.append(("TRAC:DATA? TRACE1", b"1,2"))
    with pytest.raises(DeviceError) as exc:
        a.acquire(replace(request, consistency="live"))
    assert exc.value.code == "parse_error"
