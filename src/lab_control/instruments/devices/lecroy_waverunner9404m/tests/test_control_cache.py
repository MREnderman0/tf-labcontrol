import pytest

from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError

from ..ranges import cvar_query
from .samples import binary
from .test_adapter import device
from .test_sessions import Scope


def ready():
    obj, _ = device()
    peer = Scope()
    obj.transport = peer
    obj.refresh_capabilities()
    obj.snapshot()
    return obj, peer


def test_offset_overlay_first_frame_and_stable_frame_query_contract():
    obj, peer = ready()
    req = AcquisitionRequest("waveform", (1, 2, 3, 4), consistency="live", context_id="run")
    first = obj.acquire(req)
    peer.history.clear()
    result = obj.apply(DeviceCommand("offset", {"value": 0.01}))
    assert result.control_update.complete
    assert peer.history == [
        "C1:OFST 0.01",
        "*OPC?",
        "C1:OFST?",
        "C1:TRLV?",
        *[
            cvar_query("trigger_level", 1, member)
            for member in ("GetMinValue", "GetMaxValue", "GetGrainValue")
        ],
    ]
    peer.history.clear()
    obj.refresh_capabilities()
    second = obj.acquire(req)
    assert second.validation == "cached"
    assert second.configuration["1.offset"] == 0.01 and first.configuration["1.offset"] == 0
    assert second.settings_timestamps["2.scale"] == first.settings_timestamps["2.scale"]
    assert peer.history == [f"C{ch}:WF? ALL" for ch in range(1, 5)] + ["C1:OFST?", "C1:TRLV?"]
    peer.history.clear()
    obj.acquire(req)
    assert peer.history == [f"C{ch}:WF? ALL" for ch in range(1, 5)]


def test_pending_overlay_keeps_unrelated_descriptor_checks():
    obj, peer = ready()
    req = AcquisitionRequest("waveform", consistency="live", context_id="run")
    obj.acquire(req)
    obj.apply(DeviceCommand("offset", {"value": 0.01}))
    original = peer.query
    peer.query = lambda q: binary(**{"156": ("f", 0.02)}) if ":WF?" in q else original(q)
    with pytest.raises(DeviceError) as error:
        obj.acquire(req)
    assert error.value.code == "configuration_changed" and obj._acquisition_context is None


def test_changed_affected_setting_during_frame_is_not_accepted():
    obj, peer = ready()
    req = AcquisitionRequest("waveform", consistency="live", context_id="run")
    obj.acquire(req)
    obj.apply(DeviceCommand("offset", {"value": 0.01}))
    peer.values["C1:OFST?"] = b"0.02"
    with pytest.raises(DeviceError) as error:
        obj.acquire(req)
    assert error.value.code == "configuration_changed"
    assert not obj._settings and not obj._stamps


def test_auto_is_whole_scope_without_find_and_refreshes_every_channel():
    obj, peer = ready()
    original = peer.write

    def write(q):
        if q == "C1:ASET":
            peer.history.append(q)
            peer.values["TDIV?"] = b"0.002"
        else:
            original(q)

    peer.write = write
    peer.history.clear()
    result = obj.apply(DeviceCommand("auto_setup"))
    assert result.outcome == "success" and result.control_update.complete
    assert peer.history[:2] == ["C1:ASET", "*OPC?"]
    assert peer.history.count("C1:ASET") == 1 and all("FIND" not in q for q in peer.history)
    assert "C4:VDIV?" in peer.history and obj._settings["1.timebase"] == 0.002
