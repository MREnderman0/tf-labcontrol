from copy import deepcopy

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import DeviceCommand, DeviceError, ValidationError
from lab_control.instruments.devices.coremorrow_xmt import DEFINITION
from lab_control.instruments.devices.coremorrow_xmt.adapter import CoreMorrowXmtAdapter
from lab_control.instruments.devices.coremorrow_xmt.protocol import (
    build_standard_waveform,
    build_waveform_stop,
)

from .fakes import WirePeer

PARAMETERS = {"waveform": "SIN", "frequency": 1.0, "amplitude": 0.2, "offset": 0.1}


def make_adapter(peer=None):
    return CoreMorrowXmtAdapter(peer or WirePeer(), DEFINITION.profile, ConnectionConfig("mock://e00-wire", "coremorrow.xmt"))


@pytest.mark.parametrize("channel", range(3))
@pytest.mark.parametrize("shape,code", [("SIN", "5a"), ("SQUARE", "46"), ("TRIANGLE", "53"), ("SAWTOOTH", "4a")])
def test_official_dll_encoding(channel, shape, code):
    # Header, payload and XOR independently captured from the official DLL with
    # WriteArr replaced. These are synthetic data, never a hardware transcript.
    frame = build_standard_waveform(1, channel, {**PARAMETERS, "waveform": shape})
    expected = bytes.fromhex(f"aa 01 14 0f 00 {channel:02x} {code} 00 00 07 d0 00 01 00 00 00 00 03 e8")
    assert frame[:-1] == expected
    assert frame[-1] == 0x8D ^ int(code, 16) ^ channel
    assert build_waveform_stop(1, channel) == bytes.fromhex(f"aa 01 07 11 00 {channel:02x} {0xBD ^ channel:02x}")


@pytest.mark.parametrize("change", [
    {"frequency": 0}, {"frequency": float("nan")}, {"frequency": 10000.1},
    {"amplitude": -1}, {"amplitude": 0}, {"amplitude": 121},
    {"offset": 0}, {"offset": 120}, {"offset": float("inf")}, {"waveform": "NOISE"},
    {"amplitude": 0.0001, "offset": 0.00005},
])
def test_invalid_wave_rejected_before_io(change):
    adapter = make_adapter()
    with pytest.raises(ValidationError):
        adapter.apply(DeviceCommand("standard_waveform", {**PARAMETERS, **change}))
    assert adapter.transport.history == []


def test_unknown_initial_state_does_not_write_or_claim_readback():
    adapter = make_adapter()
    state = adapter.read("control_state")
    assert state["mode"] is None
    assert state["channels"]["1"]["running"] is None
    assert state["polling_paused"]
    assert not adapter.capabilities().get("voltage").readable
    assert adapter.transport.history == []


def test_start_once_independent_stop_and_restore_all_dc():
    peer = WirePeer()
    peer.voltages = [0.1, 0.2, 0.3]
    adapter = make_adapter(peer)
    assert adapter.apply(DeviceCommand("normal_mode")).outcome == "success"
    peer.history.clear()
    for ch in (1, 2, 3):
        assert adapter.apply(DeviceCommand("standard_waveform", PARAMETERS, ch)).outcome == "success"
    original = deepcopy(adapter.waveform.journal["restore_voltage"])
    assert original == {"1": 0.1, "2": 0.2, "3": 0.3}
    assert sum(p[3] == 15 for p in peer.history) == 3
    with pytest.raises(ValidationError):
        adapter.apply(DeviceCommand("standard_waveform", PARAMETERS, 1))
    assert sum(p[3] == 15 for p in peer.history) == 3
    assert not adapter.capabilities().get("voltage").readable
    adapter.apply(DeviceCommand("waveform_stop", channel=2))
    assert set(peer.waves) == {0, 2}
    assert adapter.waveform.journal["restore_voltage"] == original
    result = adapter.apply(DeviceCommand("normal_mode"))
    assert result.outcome == "success"
    assert result.adaptation["restored"]
    assert peer.voltages == [0.1, 0.2, 0.3]
    assert not peer.waves
    assert sum(p[3] == 0 for p in peer.history) == 3
    assert adapter.capabilities().get("voltage").readable


def test_pending_state_saved_before_write_and_reconnect_never_replays():
    adapter = make_adapter()
    saved = []
    adapter.bind_control_state(lambda: None, lambda v: saved.append(deepcopy(v)))
    adapter.apply(DeviceCommand("normal_mode"))
    adapter.transport.fail_write = DeviceError("lost", "timeout")
    result = adapter.apply(DeviceCommand("standard_waveform", PARAMETERS, 1))
    assert result.outcome == "uncertain"
    assert saved[-2]["last_result"]["outcome"] == "pending"
    assert saved[-1]["last_result"]["outcome"] == "uncertain"
    assert saved[-1]["restore_voltage"]
    assert saved[-1]["last_success"]["operation"] == "normal_mode"
    assert saved[-1]["channels"]["1"]["last_result"]["outcome"] == "uncertain"
    again = make_adapter()
    again.bind_control_state(lambda: saved[-1], lambda v: saved.append(v))
    assert again.read("control_state")["channels"]["1"]["running"] is None
    assert again.read("control_state")["restore_available"]
    again.release_control()
    assert again.transport.history == []


def test_persistence_failure_prevents_write():
    adapter = make_adapter()
    adapter.waveform.running = dict.fromkeys(adapter.channels, False)
    adapter.bind_control_state(lambda: None, lambda value: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        adapter.apply(DeviceCommand("normal_mode"))
    assert adapter.transport.history == []


def test_failed_restoration_keeps_backup_and_unknown_state():
    adapter = make_adapter()
    adapter.apply(DeviceCommand("normal_mode"))
    adapter.apply(DeviceCommand("standard_waveform", PARAMETERS, 1))
    backup = deepcopy(adapter.waveform.journal["restore_voltage"])
    adapter.transport.fail_read = DeviceError("read lost", "timeout")
    result = adapter.apply(DeviceCommand("normal_mode"))
    assert result.outcome == "uncertain"
    assert adapter.waveform.journal["restore_voltage"] == backup
    assert adapter.read("control_state")["polling_paused"]


def test_normal_disconnect_restores_waveforms_owned_by_this_session():
    peer = WirePeer()
    peer.voltages = [0.1, 0.2, 0.3]
    adapter = make_adapter(peer)
    adapter.apply(DeviceCommand("normal_mode"))
    adapter.apply(DeviceCommand("standard_waveform", PARAMETERS, 2))
    adapter.release_control()
    assert not peer.waves
    assert peer.voltages == [0.1, 0.2, 0.3]
    assert not adapter.waveform.journal.get("restore_voltage")


def test_stop_timeout_keeps_unknown_state_and_does_not_repeat_write():
    adapter = make_adapter()
    adapter.transport.fail_write = DeviceError("stop lost", "timeout")
    result = adapter.apply(DeviceCommand("waveform_stop", channel=2))
    assert result.outcome == "uncertain"
    assert len(adapter.transport.history) == 1
    assert adapter.read("control_state")["channels"]["2"]["commanded_running"] is None
    assert adapter.waveform.journal["channels"]["2"]["last_result"]["outcome"] == "uncertain"
