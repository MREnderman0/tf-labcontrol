import numpy as np
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


def test_sdg_binary_is_signed_little_endian_without_appended_newline():
    samples = [-1.0] * 16 + [1.0] * 16
    payload = b"C1:WVDT WVNM,trial,WAVEDATA," + np.array([-32767] * 16 + [32767] * 16, dtype="<i2").tobytes()
    device, transport = device_for("siglent.sdg6032x", [(payload, b"")])
    assert (
        device.apply(
            DeviceCommand("upload_waveform", {"samples": samples, "name": "trial"}, confirmed=True)
        ).outcome
        == "success"
    )
    assert transport.history == [payload]


def test_sdg_disabled_modulation_does_not_implicitly_enable_output():
    device, transport = device_for("siglent.sdg6032x", [("C1:MDWV?", b"C1:MDWV STATE,OFF")])
    with pytest.raises(ValidationError):
        device.apply(DeviceCommand("am_depth", {"value": 20}))
    assert transport.history == ["C1:MDWV?"]
