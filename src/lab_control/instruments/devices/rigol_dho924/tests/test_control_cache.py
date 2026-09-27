from dataclasses import replace

import pytest

from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError

from .test_adapter import ready


def prepare_auto(peer, *, enabled=True, channels=(1, 2)):
    peer.state[":AUToset:ENAble"] = int(enabled)
    original = peer.write

    def write(payload):
        if payload == ":AUToset":
            peer.history.append(payload)
            for ch in range(1, 5):
                peer.state[f":CHAN{ch}:DISP"] = int(ch in channels)
            peer.state[":TIM:MAIN:SCAL"] = 2e-6
            peer.state[":TRIG:STAT"] = "AUTO"
        else:
            original(payload)

    peer.write = write


def test_offset_and_next_frame_read_only_dependencies_and_keep_times():
    obj, peer = ready()
    request = AcquisitionRequest("waveform", (1, 2, 3, 4), consistency="live", context_id="run")
    first = obj.acquire(request)
    peer.history.clear()
    result = obj.apply(DeviceCommand("offset", {"value": 0.01}))
    assert result.control_update.complete
    assert [q for q in peer.history if "?" in q] == ["*OPC?", ":CHAN1:OFFS?", ":SYST:ERR?", ":TRIG:EDGE:LEV?"]
    readings = {(r.channel, r.operation): r for r in result.control_update.readings}
    assert readings[2, "scale"].source == "control_cache"
    assert readings[2, "scale"].timestamp == first.settings_timestamps["2.scale"]
    peer.history.clear()
    obj.refresh_capabilities()
    after = obj.acquire(request)
    assert after.validation == "cached"
    assert after.configuration["1.offset"] == 0.01 and first.configuration["1.offset"] == 0
    assert after.settings_timestamps["2.scale"] == first.settings_timestamps["2.scale"]
    assert [q for q in peer.history if "?" in q] == [":WAV:PRE?", ":WAV:DATA?", ":WAV:PRE?"] * 4
    # A new batch and explicit full still do a complete verification.
    assert obj.acquire(replace(request, context_id="next")).validation == "full"
    assert obj.acquire(replace(request, consistency="full")).validation == "full"


@pytest.mark.parametrize(
    "op,value,expected",
    [
        ("scale", 0.1, {"1.scale", "1.offset", "1.coupling", "1.probe_attenuation", "1.trigger_level"}),
        (
            "timebase",
            2e-6,
            {"1.timebase", "1.time_position", "1.sample_rate", "1.record_length", "1.roll_enabled"},
        ),
        (
            "trigger_source",
            "CHAN2",
            {"1.trigger_source", "1.trigger_level", "1.trigger_slope", "1.trigger_type"},
        ),
    ],
)
def test_model_declares_dependencies(op, value, expected):
    obj, peer = ready()
    req = AcquisitionRequest("waveform", (1, 2), consistency="live", context_id="run")
    obj.acquire(req)
    result = obj.apply(DeviceCommand(op, {"value": value}))
    assert {f"{ch}.{name}" for ch, name in result.control_update.affected} == expected
    assert obj.acquire(req).validation == "cached"


def test_new_descriptor_still_must_match_settings_and_error_discards_cache():
    obj, peer = ready()
    req = AcquisitionRequest("waveform", consistency="live", context_id="run")
    obj.acquire(req)
    obj.apply(DeviceCommand("offset", {"value": 0.01}))
    peer.state[":CHAN1:SCAL"] = 1  # Unexpected front-panel edit, not part of the command.
    with pytest.raises(DeviceError) as error:
        obj.acquire(req)
    assert error.value.code == "configuration_changed" and obj._context_cache is None
    assert obj.acquire(req).validation == "full"


@pytest.mark.parametrize("enabled", [True, False])
def test_native_auto_once_full_refresh_and_no_unlock(enabled):
    obj, peer = ready()
    prepare_auto(peer, enabled=enabled)
    req = AcquisitionRequest("waveform", consistency="live", context_id="run")
    obj.acquire(req)
    peer.history.clear()
    result = obj.apply(DeviceCommand("auto_setup"))
    assert result.outcome == ("success" if enabled else "rejected")
    assert peer.history.count(":AUToset") == int(enabled)
    assert not any(q.startswith(":AUToset:ENAble ") for q in peer.history)
    assert obj._context_cache is None
    if enabled:
        assert result.control_update.complete
        assert obj._settings["1.timebase"] == 2e-6 and not obj._settings["3.channel_enabled"]
        assert obj.acquire(req).validation == "full"


def test_auto_timeout_is_not_replayed_or_published():
    obj, peer = ready()
    prepare_auto(peer)
    peer.fail = "*OPC?"
    result = obj.apply(DeviceCommand("auto_setup"))
    assert result.outcome == "partial" and result.control_update is None
    assert peer.history.count(":AUToset") == 1 and obj._ranges_dirty
