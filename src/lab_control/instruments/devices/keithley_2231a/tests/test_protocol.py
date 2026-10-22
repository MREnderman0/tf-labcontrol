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


def test_keithley_select_set_readback_atomic_sequence():
    device, transport = adapter(
        "keithley.2231a",
        [("INST:NSEL 2", b""), ("VOLT 2.5", b""), ("VOLT?", b"2.500"), ("SYST:ERR?", b"0,No error")],
    )
    assert device.apply(DeviceCommand("voltage", {"value": 2.5}, 2)).outcome == "success"
    assert len(transport.history) == 4
    with pytest.raises(ValidationError):
        device.apply(DeviceCommand("voltage", {"value": 6}, 3))
