from dataclasses import replace

import pytest

from lab_control.application.acquisition_pipeline import validate_recorded_batch
from lab_control.domain.instruments import AcquisitionRequest, DeviceError

from ..profile import profile
from .samples import binary, snapshot_exchanges
from .test_adapter import device


def full(channels=(1,)):
    settings = snapshot_exchanges(profile(), channels)
    return settings + [(f"C{ch}:WF? ALL", binary(ch)) for ch in channels] + settings


def grouped(channels=(1,)):
    from ..adapter import CHANNEL_SETTINGS, GLOBAL_SETTINGS
    from ..protocol import value_text

    scalar = dict(snapshot_exchanges(profile(), channels))
    result = []
    for ch, operations in ((1, GLOBAL_SETTINGS), *((ch, CHANNEL_SETTINGS) for ch in channels)):
        queries = [profile().commands[op].query.format(ch=ch) for op in operations]
        reply = b";".join((q[:-1] + " " + value_text(scalar[q])).encode() for q in queries)
        result.append((";".join(queries), reply + b"\n"))
    return result


def test_qualified_grouped_startup_and_cached_path():
    channels = (1, 2, 3, 4)
    snapshot = grouped(channels)
    waves = [(f"C{ch}:WF? ALL", binary(ch)) for ch in channels]
    adapter, wire = device(snapshot + waves + snapshot + waves)
    adapter.config = replace(adapter.config, transport="visa")
    adapter.acquisition_preparation = {"status": "ready", "before": {"response_header": "SHORT"}}
    request = AcquisitionRequest("waveform", channels, consistency="stream", context_id="grouped")
    first = adapter.acquire(request)
    assert first.validation == "full" and first.performance["logical_queries"] == 14
    second = adapter.acquire(request)
    assert second.validation == "cached" and second.performance["logical_queries"] == 4
    assert first.settings_timestamps == second.settings_timestamps and not wire.exchanges


@pytest.mark.parametrize("fault", ["count", "order", "header", "message"])
def test_grouped_reply_failure_closes_context_without_scalar_fallback(fault):
    command, raw = grouped()[0]
    fields = raw.strip().split(b";")
    if fault == "count":
        fields.pop()
    elif fault == "order":
        fields[0], fields[1] = fields[1], fields[0]
    elif fault == "header":
        fields[0] = b"0.001 S"
    else:
        fields[0] += b"\nTRDL 0"
    adapter, wire = device([(command, b";".join(fields))])
    adapter.config = replace(adapter.config, transport="visa")
    adapter.acquisition_preparation = {"status": "ready", "before": {"response_header": "SHORT"}}
    with pytest.raises(DeviceError):
        adapter.acquire(AcquisitionRequest("waveform", consistency="live", context_id="broken"))
    assert wire.history == [command] and adapter._acquisition_context is None


@pytest.mark.parametrize(
    "firmware,header,ready",
    [("9.1.0", "SHORT", True), ("9.2.0", "OFF", True), ("9.2.0", "LONG", True), ("9.2.0", "SHORT", False)],
)
def test_unqualified_header_or_firmware_selects_scalar_before_io(firmware, header, ready):
    adapter, _ = device(full())
    adapter.config = replace(adapter.config, transport="visa")
    adapter.identity = replace(adapter.identity, firmware=firmware)
    adapter.acquisition_preparation = {
        "status": "ready" if ready else "reading",
        "before": {"response_header": header},
    }
    assert not adapter.grouped_snapshot_queries()
    assert adapter.acquire(AcquisitionRequest("waveform")).performance["logical_queries"] == 37


@pytest.mark.parametrize("mode", ["live", "stream"])
def test_cached_frames_query_only_waveform_and_keep_provenance(mode):
    channels = (1, 2, 3, 4)
    wire_reads = [(f"C{ch}:WF? ALL", binary(ch)) for ch in channels]
    adapter, wire = device(full(channels) + wire_reads * 3)
    request = AcquisitionRequest("waveform", channels, consistency=mode, context_id="run-a")
    first = adapter.acquire(request)
    count = len(wire.history)
    for _ in range(3):
        frame = adapter.acquire(request)
        assert frame.validation == "cached" and frame.configuration == first.configuration
        assert frame.settings_timestamps == first.settings_timestamps
        assert adapter.acquisition_readings(frame) == ()
        assert frame.performance["logical_queries"] == 4
        if mode == "stream":
            validate_recorded_batch(frame)
        else:
            with pytest.raises(DeviceError):
                validate_recorded_batch(frame)
    assert wire.history[count:] == [command for command, _ in wire_reads] * 3


@pytest.mark.parametrize("change", ["run", "channels", "mode", "refresh"])
def test_context_boundaries_rebuild_full_snapshot(change):
    channels = (1, 2) if change == "channels" else (1,)
    adapter, wire = device(full() + full(channels))
    request = AcquisitionRequest("waveform", consistency="live", context_id="run-a")
    adapter.acquire(request)
    request = replace(
        request,
        channels=channels,
        context_id="run-b" if change == "run" else "run-a",
        consistency="stream" if change == "mode" else "live",
    )
    if change == "refresh":
        adapter.invalidate_acquisition_context("explicit_refresh")
    assert adapter.acquire(request).validation == "full"
    assert not wire.exchanges


@pytest.mark.parametrize(
    "offset,fmt,value",
    [
        (156, "f", 0.02),
        (176, "f", 0.002),
        (324, "h", 20),
        (326, "h", 4),
        (328, "f", 10),
        (332, "h", 13),
        (334, "h", 1),
        (336, "f", 0.5),
        (340, "f", 0.1),
    ],
)
def test_valid_descriptor_changes_invalidate_and_do_not_publish(offset, fmt, value):
    adapter, wire = device(full() + [("C1:WF? ALL", binary(**{str(offset): (fmt, value)}))] + full())
    request = AcquisitionRequest("waveform", consistency="live", context_id="run")
    adapter.acquire(request)
    with pytest.raises(DeviceError) as error:
        adapter.acquire(request)
    assert error.value.code == "configuration_changed"
    assert "descriptor" in str(error.value) and adapter._acquisition_context is None
    assert adapter.acquire(request).validation == "full" and not wire.exchanges


def test_time_jitter_is_not_a_configuration_change_and_acquisitions_are_counted():
    stamp = {
        "296": ("d", 12.25),
        "304": ("b", 30),
        "305": ("b", 12),
        "306": ("b", 1),
        "307": ("b", 9),
        "308": ("h", 2026),
    }
    adapter, wire = device(
        full()
        + [
            ("C1:WF? ALL", binary(**stamp)),
            ("C1:WF? ALL", binary(**stamp, **{"180": ("d", -0.0010000001)})),
            ("C1:WF? ALL", binary(**{**stamp, "296": ("d", 12.250000001)})),
        ]
    )
    request = AcquisitionRequest("waveform", consistency="stream", context_id="run")
    adapter.acquire(request)
    assert adapter.acquire(request).performance["acquisition_observations"] == {"1": "first"}
    assert adapter.acquire(request).performance["acquisition_observations"] == {"1": "repeat"}
    assert adapter.acquire(request).performance["acquisition_observations"] == {"1": "changed"}
    assert not wire.exchanges


def test_truncated_changed_payload_is_a_parse_error_and_no_retry():
    adapter, wire = device(full() + [("C1:WF? ALL", binary(**{"328": ("f", 10)})[:-5])])
    request = AcquisitionRequest("waveform", consistency="live", context_id="run")
    adapter.acquire(request)
    with pytest.raises(DeviceError) as error:
        adapter.acquire(request)
    assert error.value.code == "parse_error" and adapter._acquisition_context is None
    assert wire.history.count("C1:WF? ALL") == 2


def test_transfer_window_and_missing_context_are_rejected():
    from ..protocol import waveform

    for field, value in ((132, 1), (136, 4)):
        with pytest.raises(DeviceError, match="sparsed"):
            waveform(binary(**{str(field): ("i", value)}), 1)
    adapter, wire = device()
    with pytest.raises(DeviceError):
        adapter.acquire(AcquisitionRequest("waveform", consistency="live"))
    assert not wire.history
