import json
from pathlib import Path

import numpy as np
import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import (
    AcquisitionRequest,
    DeviceCommand,
    DeviceIdentity,
    Measurement,
    ValidationError,
)
from lab_control.instruments.base import ScpiAdapter
from lab_control.instruments.phase_noise import PhaseNoiseAdapter
from lab_control.instruments.power_supplies import PowerAdapter
from lab_control.instruments.registry import PROFILES, describe
from lab_control.instruments.signal_sources import SignalGeneratorAdapter
from lab_control.storage.touchstone import export_touchstone
from lab_control.transport.base import MockTransport


def device_for(adapter_id, exchanges):
    transport = MockTransport(exchanges)
    transport.open()
    profile = PROFILES[adapter_id]
    cls = (
        SignalGeneratorAdapter
        if profile.category == "signal_generator"
        else PowerAdapter
        if profile.category == "power_supply"
        else ScpiAdapter
    )
    return cls(transport, profile, ConnectionConfig("mock://fixture", adapter_id)), transport


FIXTURES = sorted((Path(__file__).parents[1] / "fixtures/protocols").glob("*/control.json"))


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.parent.name)
def test_manufacturer_control_fixtures(path):
    case = json.loads(path.read_text(encoding="utf-8"))
    device, transport = device_for(
        case["adapter"], [(cmd, reply.encode()) for cmd, reply in case["exchanges"]]
    )
    result = device.apply(DeviceCommand(case["operation"], case["parameters"], case["channel"], True))
    assert result.outcome == "success", result
    assert not transport.exchanges


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


def test_amplitude_units_and_envelope_are_checked_before_any_write():
    device, transport = device_for("tek.afg31252", [("SOUR1:VOLT:UNIT?", b"VPP"), ("SOUR1:VOLT:OFFS?", b"4")])
    with pytest.raises(ValidationError):
        device.apply(DeviceCommand("amplitude", {"value": 4}))
    assert len(transport.history) == 2 and all(str(cmd).endswith("?") for cmd in transport.history)


def test_sma_list_upload_is_three_ordered_writes_and_never_starts_output():
    exchanges = [
        ("SOUR:LIST:SEL 'trial'", b""),
        ("SOUR:LIST:FREQ 1000000,2000000", b""),
        ("SOUR:LIST:POW -40,-30", b""),
        ("SYST:ERR?", b"0,No error"),
    ]
    device, transport = device_for("rs.sma100b", exchanges)
    result = device.apply(
        DeviceCommand(
            "upload_list", {"name": "trial", "frequencies": [1e6, 2e6], "powers": [-40, -30]}, confirmed=True
        )
    )
    assert result.outcome == "success" and result.completed_steps == 3
    assert not transport.exchanges


def test_gpd_binary_status_is_not_ascii_or_a_scpi_error_queue():
    device, _ = device_for("gwinstek.gpd4303s", [("STATUS?", b"\xa7")])
    status = device.read("status_register")
    assert status["output_enabled"] and status["ch1_mode"] == "CV" and status["baud_bits"] == 2


def test_tserve_mhz_conversion_and_labelled_allan_deviation():
    transport = MockTransport(
        [("set inputfreq 10", b""), ("show adev", b"tau: 0.1 adev: 5.2e-12\r\ntau: 0.2 adev: 4e-12\r\n")]
    )
    transport.open()
    device = PhaseNoiseAdapter(
        ConnectionConfig("tcp://localhost:1299", "microchip.53100a", "tserve", identity_confirmed=True),
        transport,
    )
    device.identity = DeviceIdentity("Microchip", "53100A", "TEST")
    assert device.apply(DeviceCommand("input_frequency", {"value": 10e6})).outcome == "success"
    batch = device.acquire(AcquisitionRequest(quantity="stability"))
    np.testing.assert_allclose(batch.measurements[0].axis, [0.1, 0.2])
    assert not transport.exchanges


def test_sdk_capability_inspection_never_loads_a_dll():
    for adapter in ("pico.6404e", "thorlabs.pm100d", "microchip.53100a"):
        assert describe(ConnectionConfig("metadata://only", adapter)).operations


def test_touchstone_order_and_reject_missing_or_mismatched_traces(tmp_path):
    traces = {
        name: Measurement(
            name, "ratio", [complex(i, -i), complex(i + 1, -i - 1)], axis=[1e6, 2e6], axis_unit="Hz"
        )
        for i, name in enumerate(("S11", "S21", "S12", "S22"), 1)
    }
    path = export_touchstone(tmp_path / "网络实测.s2p", traces)
    data = np.loadtxt(path, comments=("!", "#"))
    np.testing.assert_array_equal(data[0], [1e6, 1, -1, 2, -2, 3, -3, 4, -4])
    with pytest.raises(ValidationError):
        export_touchstone(tmp_path / "bad.s2p", {"S21": traces["S21"]})
