from types import SimpleNamespace

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import DeviceError
from lab_control.instruments.devices.microchip_53100a import identity
from lab_control.instruments.registry import build_scpi_adapter
from lab_control.transport.base import MockTransport


@pytest.mark.parametrize("raw", ["TEST123:12345.6", "TEST123 MOD4:12345.6", "TEST123"])
def test_live_usb_identity_uses_vendor_serial_normalization(monkeypatch, raw):
    evidence = {
        "strings": {
            "manufacturer": "Microchip Technology, Inc.",
            "product": "Microchip 53100A",
            "serial": raw,
        }
    }
    monkeypatch.setattr(identity, "read_identity", lambda *_: evidence)
    actual = identity.verify_usb_identity("synthetic selected instance", "TEST123")
    assert actual["serial"] == "TEST123" and actual["strings"]["serial"] == raw
    assert actual["confidence"] == "usb_descriptor"


@pytest.mark.parametrize(
    "change", [{"serial": "OTHER:12345.6"}, {"product": "Other instrument"}, {"manufacturer": "Other vendor"}]
)
def test_usb_identity_requires_full_target_match(monkeypatch, change):
    strings = {
        "manufacturer": "Microchip Technology, Inc.",
        "product": "Microchip 53100A",
        "serial": "TEST123",
    }
    monkeypatch.setattr(identity, "read_identity", lambda *_: {"strings": {**strings, **change}})
    with pytest.raises(DeviceError) as caught:
        identity.verify_usb_identity("synthetic selected instance", "TEST123")
    assert caught.value.code == "identity_conflict"


def test_only_owned_idle_server_can_use_live_usb_identity():
    wire = MockTransport(
        [
            ("show version", b"Model: Microchip 53100A\nSoftware: Version 1.78"),
            ("show serial", b"Error: No active connection"),
            ("show state", b"Ready\nTime Constant: Infinite"),
        ]
    )
    wire.open()
    config = ConnectionConfig("mock://selected", "microchip.53100a", expected_serial="TEST123")
    client = build_scpi_adapter(config, wire)
    calls = []

    def verify():
        calls.append("live USB read")
        return {"serial": "TEST123", "source": "USB GET_DESCRIPTOR"}

    client.server = SimpleNamespace(verify_identity=verify)
    result = client.identify()
    assert result.serial == "TEST123" and result.confidence == "usb_descriptor"
    assert result.firmware == "" and calls == ["live USB read"]
    assert client.capability_details["hardware_acquisition_open"] is False
    assert not wire.exchanges
