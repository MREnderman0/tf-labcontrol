import pytest

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import DeviceCommand, DeviceError, Unsupported
from lab_control.instruments.registry import build_scpi_adapter

from .fake import Peer


def device():
    peer = Peer()
    adapter = build_scpi_adapter(ConnectionConfig(peer.resource, "rs.rtm3004", "mock"), peer)
    return adapter, peer


def test_auto_sends_one_native_command_then_publishes_fresh_ranges_and_readings():
    adapter, peer = device()
    result = adapter.apply(DeviceCommand("auto_setup", confirmed=True))
    assert result.outcome == "success" and result.control_update.complete
    assert peer.history.count("AUT") == 1
    assert peer.timeouts == [("*OPC?", 30.0)]
    assert [command for command in peer.history if "?" not in command] == ["AUT"]
    readings = {(r.channel, r.operation): r for r in result.control_update.readings}
    assert readings[1, "timebase"].value == 0.0002
    assert readings[4, "channel_enabled"].value is False
    assert readings[1, "trigger_mode"].value == "AUTO"
    assert all(r.source == "instrument" and r.timestamp for r in readings.values())
    assert adapter.capabilities_for_channel(1).get("scale").parameters["value"].range_source == "instrument"


@pytest.mark.parametrize("fail", ["AUT", "*OPC?", "CHAN1:SCAL? MIN"])
def test_timeout_or_post_read_failure_has_no_replay_or_complete_update(fail):
    adapter, peer = device()
    peer.fail = fail
    result = adapter.apply(DeviceCommand("auto_setup", confirmed=True))
    assert result.outcome in {"uncertain", "partial"}
    assert result.control_update is None
    assert peer.history.count("AUT") == 1 and adapter.capability_error
    assert not adapter.capabilities_for_channel(1).get("auto_setup").writable
    if fail == "*OPC?":
        assert peer.closed and peer.history[-1] == "*OPC?"


def test_explicit_rejection_keeps_error_and_does_not_replay_or_claim_readback():
    adapter, peer = device()
    peer.auto_error = '-221,"Settings conflict"'
    result = adapter.apply(DeviceCommand("auto_setup", confirmed=True))
    assert result.outcome == "rejected" and "Settings conflict" in result.error
    assert not peer.closed and result.control_update is None
    assert peer.history.count("AUT") == 1


@pytest.mark.parametrize("response", [b"0", b"nan", b"invalid"])
def test_invalid_completion_remains_uncertain(response):
    adapter, peer = device()
    peer.completion = response
    result = adapter.apply(DeviceCommand("auto_setup", confirmed=True))
    assert result.outcome == "partial" and result.control_update is None
    assert peer.history.count("AUT") == 1 and "SYST:ERR?" not in peer.history


def test_confirmation_and_bounded_transport_are_required_before_any_io():
    adapter, peer = device()
    with pytest.raises(DeviceError, match="confirmation"):
        adapter.apply(DeviceCommand("auto_setup"))
    peer.query_timeout = None
    with pytest.raises(Unsupported):
        adapter.apply(DeviceCommand("auto_setup", confirmed=True))
    assert peer.history == []
