import pytest

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import DeviceCommand, ValidationError
from lab_control.instruments.registry import build_scpi_adapter
from lab_control.transport.base import MockTransport


def device_for(adapter_id, exchanges):
    wire = MockTransport(exchanges)
    wire.open()
    return build_scpi_adapter(ConnectionConfig("mock://fixture", adapter_id), wire), wire


adapter = device_for


def test_spd_uses_channel_argument_and_no_invented_error_queue():
    device, transport = adapter("siglent.spd4306x", [("VOLT CH2,12", b""), ("VOLT:SET? CH2", b"12")])
    assert device.apply(DeviceCommand("voltage", {"value": 12}, 2)).outcome == "success"
    assert not transport.exchanges
    with pytest.raises(ValidationError):
        device.apply(DeviceCommand("voltage", {"value": 12}, 1))
