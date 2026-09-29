"""Mode diagnostics use existing snapshots and never silently reconfigure hardware."""

import pytest

from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, Unsupported

from .fake import adapter


@pytest.mark.parametrize(
    "settings, expected",
    [
        ({":TIM:MODE": "ROLL", ":TIM:ROLL": 1, ":TIM:MAIN:SCAL": 20}, "20 ms/格或更短"),
        ({":TIM:MODE": "ROLL"}, "切回 MAIN 主时基"),
        ({":TIM:MODE": "XY"}, "时基模式 XY"),
        ({":TIM:XY:ENAB": 1}, "XY 显示已开启"),
        ({":TIM:DEL:ENAB": 1}, "延迟扫描已开启"),
        ({":ACQ:TYPE": "PEAK"}, "峰值检测（PEAK）"),
        ({":ACQ:TYPE": "ULTR"}, "超快分段采集（ULTR）"),
        ({":ACQ:TYPE": "UNKNOWN"}, "未支持的采集方式（UNKNOWN）"),
    ],
)
def test_unsupported_mode_names_actual_blocker_without_writes(settings, expected):
    obj, peer = adapter()
    peer.state.update(settings)
    peer.history.clear()
    with pytest.raises(Unsupported) as error:
        obj.acquire(AcquisitionRequest("waveform", consistency="live", context_id="mode-test"))
    assert expected in str(error.value)
    assert error.value.code == "unsupported"
    assert all(command.endswith("?") for command in peer.history)
    assert not any(command.startswith(":WAV:") for command in peer.history)
    assert obj._context_cache is None


def test_combined_blockers_are_reported_without_hiding_delay():
    obj, peer = adapter()
    peer.state.update({":ACQ:TYPE": "PEAK", ":TIM:DEL:ENAB": 1})
    with pytest.raises(Unsupported) as error:
        obj.acquire(AcquisitionRequest("waveform"))
    assert "峰值检测" in str(error.value) and "延迟扫描" in str(error.value)
    assert "ROLL" not in str(error.value)


@pytest.mark.parametrize("acquisition", ["NORM", "AVER"])
def test_auto_roll_preference_is_not_itself_an_unsupported_mode(acquisition):
    obj, peer = adapter()
    peer.state.update({":TIM:ROLL": 1, ":ACQ:TYPE": acquisition})
    obj.prepare_acquisition()
    request = AcquisitionRequest("waveform", consistency="live", context_id="main")
    assert obj.acquire(request).validation == "full"
    peer.history.clear()
    assert obj.acquire(request).validation == "cached"
    assert peer.history == [":WAV:PRE?", ":WAV:DATA?", ":WAV:PRE?"]


def test_timebase_readback_updates_mode_cache_without_unrelated_queries():
    obj, peer = adapter()
    obj.refresh_capabilities()
    obj.prepare_acquisition()
    request = AcquisitionRequest("waveform", consistency="live", context_id="main")
    first = obj.acquire(request)
    peer.history.clear()
    result = obj.apply(DeviceCommand("timebase", {"value": 2e-6}))
    assert result.outcome == "success" and result.control_update is not None
    assert peer.history.count(":TIM:MODE?") == 1
    assert not any(command.startswith(":CHAN") for command in peer.history)
    reading = next(r for r in result.control_update.readings if r.operation == "timebase_mode")
    assert reading.value == "MAIN" and reading.source == "instrument"
    peer.history.clear()
    frame = obj.acquire(request)
    assert frame.validation == "cached"
    assert frame.settings_timestamps["1.offset"] == first.settings_timestamps["1.offset"]
    assert frame.settings_timestamps["1.timebase_mode"] == reading.timestamp
    assert peer.history == [":WAV:PRE?", ":WAV:DATA?", ":WAV:PRE?"]


def test_explicit_connection_preparation_returns_from_roll():
    obj, peer = adapter()
    peer.state.update({":TIM:MODE": "ROLL", ":TIM:ROLL": 1, ":TIM:MAIN:SCAL": 20})
    obj.refresh_capabilities()
    obj.prepare_acquisition()
    assert obj._settings["1.timebase_mode"] == "MAIN"
    assert peer.state[":TIM:MAIN:SCAL"] == 0.02
    assert peer.history.count(":TIM:MAIN:SCAL 0.02") == 1
    assert peer.history.count(":TIM:MODE MAIN") == 1
    assert not any(command.startswith(":TIM:ROLL ") for command in peer.history)
    assert len(obj.acquire(AcquisitionRequest("waveform")).measurements[0].values) == 1000
