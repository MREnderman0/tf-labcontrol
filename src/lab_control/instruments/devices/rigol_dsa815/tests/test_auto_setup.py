import pytest

from lab_control.domain.instruments import DeviceCommand, DeviceError
from lab_control.domain.spectrum import SETTINGS

from .test_protocol import adapter


def test_auto_waits_for_completion_and_reads_all_settings(monkeypatch):
    device = adapter(
        [
            (":SENSe:POWer:ATUNe", b""),
            ("*OPC?", b"0"),
            ("*OPC?", b"1"),
            ("SYST:ERR?", b'0,"No error"'),
        ]
    )
    queried = []
    monkeypatch.setattr(device, "read", lambda name: (queried.append(name), 1)[1])
    result = device.apply(DeviceCommand("auto_setup"))
    assert result.outcome == "success"
    assert set(result.readback) == set(SETTINGS) and queried == list(SETTINGS)
    assert device.transport.history.count(":SENSe:POWer:ATUNe") == 1


@pytest.mark.parametrize("response", [b"garbage", b"timeout", b"0"])
def test_auto_uncertain_completion_is_not_replayed(response, monkeypatch):
    device = adapter()
    writes = []
    monkeypatch.setattr(device.transport, "write", writes.append)

    def query(_command):
        if response == b"timeout":
            raise DeviceError("fixture timeout", "timeout")
        return response

    monkeypatch.setattr(device.transport, "query", query)
    times = iter((0, 31))
    monkeypatch.setattr(
        "lab_control.instruments.devices.rigol_dsa815.auto_setup.time.monotonic", lambda: next(times)
    )
    result = device.apply(DeviceCommand("auto_setup"))
    assert result.outcome == "partial" and writes == [":SENSe:POWer:ATUNe"]


def test_auto_readback_failure_keeps_partial_result(monkeypatch):
    device = adapter(
        [
            (":SENSe:POWer:ATUNe", b""),
            ("*OPC?", b"1"),
            ("SYST:ERR?", b'0,"No error"'),
        ]
    )

    def fail(name):
        raise DeviceError("readback failed", "timeout")

    monkeypatch.setattr(device, "read", fail)
    assert device.apply(DeviceCommand("auto_setup")).outcome == "partial"
