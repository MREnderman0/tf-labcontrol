from dataclasses import replace

import pytest

from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError, ValidationError

from ..profile import profile
from ..ranges import (
    EDGE_POINT_RESERVE,
    MEMORY_QUERIES,
    POINTS_MAX,
    cvar_query,
    fixed_rate_timebase,
    read_range,
)
from .samples import snapshot_exchanges
from .test_adapter import device
from .test_sessions import Scope


@pytest.mark.parametrize(
    "operation,requested,actual,grain",
    [
        ("offset", 0.00006, 0.0001, 0.0001),
        ("offset", -0.00546, -0.0055, 0.0001),
        ("time_position", 1.73e-6, 1.74e-6, 2e-8),
        ("record_length", 2400, 2500, 0),
    ],
)
def test_logged_quantization_needs_cvar_evidence_and_one_write(operation, requested, actual, grain):
    spec = profile().commands[operation]
    preflight = (
        [("C1:CPL?", b"D1M"), ("C1:ATTN?", b"1"), ("C1:VDIV?", b"0.002"), ("C1:OFST?", b"0")]
        if operation == "offset"
        else [("TDIV?", b"1e-6")]
        if operation == "time_position"
        else []
    )
    write = spec.writes[0].format(ch=1, value=f"{requested:.12g}")
    adapter, wire = device(
        preflight
        + [
            (write, b""),
            ("*OPC?", b"1"),
            (spec.query.format(ch=1), str(actual).encode()),
            (cvar_query(operation, 1, "GetRequestedValue"), str(requested).encode()),
            (cvar_query(operation, 1, "GetAdaptedValue"), str(actual).encode()),
        ]
    )
    adapter._ranges = {(1, operation): replace(spec.capability.parameters["value"], resolution=grain)}
    result = adapter.apply(DeviceCommand(operation, {"value": requested}))
    assert result.outcome == "success" and result.readback == actual
    assert result.adaptation["source"] == "instrument_cvar"
    assert wire.history.count(write) == 1 and not wire.exchanges


def test_100ms_request_20ms_actual_still_fails_without_query_or_retry():
    adapter, wire = device([("TDIV 0.1", b""), ("*OPC?", b"1"), ("TDIV?", b"0.02")])
    adapter._ranges = {(1, "timebase"): profile().commands["timebase"].capability.parameters["value"]}
    result = adapter.apply(DeviceCommand("timebase", {"value": 0.1}))
    assert result.outcome == "rejected" and not wire.exchanges


def test_completion_barrier_precedes_readback_and_timeout_never_replays():
    adapter, wire = device([("TDIV 0.1", b""), ("*OPC?", DeviceError("timeout", "timeout"))])
    result = adapter.apply(DeviceCommand("timebase", {"value": 0.1}))
    assert result.outcome == "partial" and wire.history == ["TDIV 0.1", "*OPC?"]


def test_completion_wait_has_an_upper_bound(monkeypatch):
    adapter, wire = device()
    adapter.config = replace(adapter.config, timeout=60)
    waits = []
    monkeypatch.setattr(
        wire, "query_timeout", lambda query, seconds: waits.append((query, seconds)) or b"1", raising=False
    )
    adapter.complete_setting(DeviceCommand("timebase", {"value": 1e-6}))
    assert waits == [("*OPC?", 60)]


def test_live_timebase_limit_rejects_before_any_write():
    adapter, wire = device()
    adapter._ranges = {
        (1, "timebase"): replace(profile().commands["timebase"].capability.parameters["value"], maximum=0.02)
    }
    with pytest.raises(ValidationError, match="above 0.02"):
        adapter.apply(DeviceCommand("timebase", {"value": 0.1}))
    assert wire.history == []


def test_cvar_ranges_are_cached_and_dependency_changes_invalidate():
    adapter, _ = device()
    wire = Scope()
    adapter.transport = wire
    adapter.refresh_capabilities()
    count = len(wire.history)
    adapter.refresh_capabilities()
    adapter.capabilities_for_channel(2)
    assert len(wire.history) == count
    adapter.remember({"2.scale": 0.002})
    adapter.refresh_capabilities()
    assert len(wire.history) > count
    assert (
        adapter.capabilities_for_channel(2)
        .get("offset")
        .parameters["value"]
        .range_source.startswith("instrument")
    )


@pytest.mark.parametrize("points,rate", [(0, 1e9), (1.5, 1e9), (5000, 0)])
def test_invalid_actual_memory_does_not_publish_partial_capabilities(points, rate):
    adapter, _ = device(
        [
            (MEMORY_QUERIES["mode"], b"FixedSampleRate"),
            (MEMORY_QUERIES["points"], str(points).encode()),
            (MEMORY_QUERIES["sample_rate"], str(rate).encode()),
        ]
    )
    with pytest.raises(DeviceError, match="memory/rate"):
        adapter.refresh_capabilities()
    assert "memory" not in adapter.capability_details and adapter.capability_error


def test_memory_range_intersects_software_and_preserves_current_value():
    replies = [
        (cvar_query("record_length", 1, member), str(value).encode())
        for member, value in (("GetMinValue", 500), ("GetMaxValue", 128_000_000), ("GetGrainValue", 1))
    ]
    _, wire = device(replies)
    spec = read_range(
        wire, "record_length", 1, profile().commands["record_length"].capability.parameters["value"]
    )
    assert (spec.minimum, spec.maximum) == (1000, 10_000_000)
    assert all("?" in q for q in wire.history)


def test_large_memory_rejected_before_requesting_waveform():
    adapter, wire = device(snapshot_exchanges(profile(), overrides={"1.record_length": b"MSIZ 64MA"}))
    with pytest.raises(DeviceError, match="采集点数") as exc:
        adapter.acquire(AcquisitionRequest("waveform"))
    assert exc.value.code == "acquisition_budget_exceeded"
    assert not any("WF?" in q for q in wire.history)


def test_fixed_sample_rate_cannot_hide_actual_memory_behind_small_msiz():
    adapter, wire = device(snapshot_exchanges(profile()))
    adapter.capability_details["memory"] = {"mode": "FixedSampleRate", "points": 64_000_000}
    with pytest.raises(DeviceError, match="64,000,000") as exc:
        adapter.acquire(AcquisitionRequest("waveform"))
    assert exc.value.code == "acquisition_budget_exceeded"
    assert not any("WF?" in q for q in wire.history)


@pytest.mark.parametrize("rate,maximum", [(500e6, 0.001), (1e9, 0.0005), (100e6, 0.005), (5e6, 0.02)])
def test_fixed_rate_timebase_limit_fits_complete_points_and_hardware(rate, maximum):
    spec = replace(profile().commands["timebase"].capability.parameters["value"], maximum=0.02)
    effective = fixed_rate_timebase(spec, rate)
    assert effective.maximum == maximum
    assert 10 * maximum * rate + EDGE_POINT_RESERVE <= POINTS_MAX
    assert effective.minimum == spec.minimum and "sampling_budget" in effective.range_source


def test_rate_limits_refresh_without_writes_and_stop_invalid_timebase_before_io():
    class FixedScope(Scope):
        sample_rate = 500e6
        memory_mode = "FixedSampleRate"

        def query(self, command):
            if command in {MEMORY_QUERIES["mode"], MEMORY_QUERIES["sample_rate"]}:
                self.history.append(command)
                return (
                    self.memory_mode if command == MEMORY_QUERIES["mode"] else str(self.sample_rate)
                ).encode()
            return super().query(command)

    adapter, _ = device()
    wire = FixedScope()
    adapter.transport = wire
    adapter.remember({"1.timebase": 0.02})
    adapter.refresh_capabilities()
    assert adapter.capabilities_for_channel(1).get("timebase").parameters["value"].maximum == 0.001
    count = len(wire.history)
    with pytest.raises(ValidationError):
        adapter.apply(DeviceCommand("timebase", {"value": 0.02}))
    assert len(wire.history) == count
    wire.sample_rate = 1e9
    adapter.refresh_capabilities()  # Cached until the known change/explicit refresh.
    assert len(wire.history) == count
    adapter.invalidate_acquisition_context("explicit_refresh")
    adapter.refresh_capabilities()
    assert adapter.capabilities_for_channel(1).get("timebase").parameters["value"].maximum == 0.0005
    assert adapter._settings["1.timebase"] == 0.02  # Never alter the original value.
    wire.memory_mode = "SetMaximumMemory"
    adapter.invalidate_acquisition_context("explicit_refresh")
    adapter.refresh_capabilities()
    assert (
        "sampling_budget"
        not in adapter.capabilities_for_channel(1).get("timebase").parameters["value"].range_source
    )
    assert all("?" in q for q in wire.history)
