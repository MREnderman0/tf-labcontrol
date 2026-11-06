from dataclasses import replace

import numpy as np
import pytest

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError
from lab_control.instruments.registry import build_scpi_adapter
from lab_control.transport.base import ieee_block

from .fake import Peer


def connected():
    peer = Peer()
    obj = build_scpi_adapter(ConnectionConfig(peer.resource, "rs.rtm3004", "mock"), peer)
    obj.identity = obj.identify()
    return obj, peer


@pytest.mark.parametrize("mode", ["live", "stream"])
def test_stable_context_preserves_parameter_times_but_reads_new_samples(mode, monkeypatch):
    obj, peer = connected()
    request = AcquisitionRequest("waveform", (1, 2), consistency=mode, context_id="batch-one")
    count = 0
    original = peer.query_binary

    def data(command, limit):
        nonlocal count
        original(command, limit)
        count += 1
        return ieee_block(np.full(64, count / 100, dtype="<f4").tobytes())

    monkeypatch.setattr(peer, "query_binary", data)
    first = obj.acquire(request)
    assert first.validation == "full" and obj.acquisition_readings(first)
    peer.history.clear()
    second = obj.acquire(request)
    assert second.validation == "cached" and not obj.acquisition_readings(second)
    assert second.settings_timestamps == first.settings_timestamps
    assert second.configuration == first.configuration
    assert all(
        not np.array_equal(a.values, b.values) for a, b in zip(first.measurements, second.measurements)
    )
    assert peer.history.count("CHAN1:DATA?") == peer.history.count("CHAN2:DATA?") == 1
    assert all(":DATA" in c or c in {"ACQ:STAT?", "FORM?", "FORM:BORD?"} for c in peer.history)
    assert all("?" in c for c in peer.history)


@pytest.mark.parametrize("change", ["batch", "channels", "mode", "full", "refresh", "auto"])
def test_new_context_or_control_rebuilds_complete_snapshot(change):
    obj, peer = connected()
    request = AcquisitionRequest("waveform", (1,), consistency="live", context_id="one")
    obj.acquire(request)
    assert obj.acquire(request).validation == "cached"
    if change == "batch":
        request = replace(request, context_id="two")
    elif change == "channels":
        request = replace(request, channels=(1, 2))
    elif change == "mode":
        request = replace(request, consistency="stream")
    elif change == "full":
        request = replace(request, consistency="full")
    elif change == "refresh":
        obj.refresh_capabilities()
    else:
        assert obj.apply(DeviceCommand("auto_setup", confirmed=True)).outcome == "success"
    peer.history.clear()
    frame = obj.acquire(request)
    assert frame.validation == "full" and "CHAN1:SCAL?" in peer.history


def test_time_grid_change_invalidates_context_before_new_configuration_is_displayed():
    obj, peer = connected()
    request = AcquisitionRequest("waveform", (1,), consistency="live", context_id="one")
    obj.acquire(request)
    peer.values[1, "timebase"] = 0.002
    with pytest.raises(DeviceError) as caught:
        obj.acquire(request)
    assert caught.value.code == "configuration_changed" and obj._context_cache is None
    frame = obj.acquire(request)
    assert frame.validation == "full" and frame.configuration["1.timebase"] == 0.002


def test_front_panel_vertical_edit_requires_explicit_refresh_without_faking_freshness():
    obj, peer = connected()
    request = AcquisitionRequest("waveform", (1,), consistency="live", context_id="one")
    first = obj.acquire(request)
    peer.values[1, "scale"] = 0.5
    cached = obj.acquire(request)
    assert cached.configuration["1.scale"] == first.configuration["1.scale"]
    assert cached.settings_timestamps == first.settings_timestamps and cached.validation == "cached"
    obj.invalidate_acquisition_context("explicit_refresh")
    assert obj.acquire(request).configuration["1.scale"] == 0.5


def test_empty_frame_invalidates_context_and_is_not_returned():
    obj, peer = connected()
    request = AcquisitionRequest("waveform", (1,), consistency="stream", context_id="one")
    obj.acquire(request)
    peer.empty_channels.add(1)
    with pytest.raises(DeviceError) as caught:
        obj.acquire(request)
    assert caught.value.code == "waveform_unavailable" and obj._context_cache is None
    peer.empty_channels.clear()
    assert obj.acquire(request).validation == "full"
