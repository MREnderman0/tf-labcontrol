"""Correlated state checks use a model stub; rejected requests never write."""

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import DeviceCommand, DeviceError
from lab_control.instruments.registry import build_scpi_adapter
from lab_control.transport.base import MockTransport


@pytest.fixture
def model(monkeypatch):
    transport = MockTransport([])
    device = build_scpi_adapter(ConnectionConfig("mock://dg", "rigol.dg5504pro"), transport)
    values = dict(
        waveform="SIN",
        frequency=1000,
        amplitude=0.2,
        offset=0,
        amplitude_unit="VPP",
        load=50,
        advanced=0,
        am_enabled=0,
        fm_enabled=0,
        pm_enabled=0,
        burst=0,
        sweep=0,
        square_fast=0,
        pulse_width=0.00025,
        pulse_duty=25,
        sweep_start=1000,
        sweep_stop=2000,
        fm_deviation=100,
        am_source="INT",
        fm_source="INT",
        pm_source="INT",
    )
    reads = []

    def read(name, channel=1):
        reads.append((name, channel))
        if name == "track":
            return "OFF"
        if name.endswith("coupling"):
            return 0
        return values[name]

    monkeypatch.setattr(device, "read", read)
    monkeypatch.setattr(device, "bounds", lambda op, ch: (-1e10, 1e10))
    return device, transport, values, reads


@pytest.mark.parametrize(
    "changes,operation,value",
    [
        ({"amplitude": 10}, "frequency", 400e6),
        ({"offset": 4.99}, "amplitude", 0.2),
        ({"load": 75}, "amplitude", 0.1),
        ({"amplitude_unit": "VRMS"}, "offset", 0),
        ({"fm_enabled": 1, "fm_deviation": 2000}, "frequency", 1000),
        ({"fm_deviation": 2000}, "fm_enabled", True),
        ({"am_enabled": 1}, "fm_enabled", True),
        ({"waveform": "RAMP", "frequency": 3e6}, "burst", True),
        ({"waveform": "SQU", "square_fast": 1}, "am_enabled", True),
        ({"waveform": "RAMP", "sweep_stop": 6e6}, "sweep", True),
        ({"waveform": "PULS"}, "pulse_width", 0.001),
        ({"waveform": "PULS", "frequency": 100e6}, "pulse_duty", 25),
        ({"waveform": "SQU"}, "pulse_width", 0.00025),
        ({"waveform": "DC"}, "frequency", 1000),
        ({"frequency": 400e6, "amplitude": 2}, "output", True),
        ({"advanced": 1}, "output", True),
        ({"pm_source": "EXT"}, "pm_frequency", 100),
    ],
)
def test_correlated_rejections_never_send_setting(model, changes, operation, value):
    device, transport, values, reads = model
    values.update(changes)
    with pytest.raises(DeviceError):
        device.apply(DeviceCommand(operation, {"value": value}, 4, True))
    assert not transport.history
    assert all(
        channel == 4 or name in {"track", "frequency_coupling", "amplitude_coupling", "phase_coupling"}
        for name, channel in reads
    )


def test_output_checks_experiment_limit(model):
    device, transport, values, _ = model
    device.config.limits["frequency"] = 500
    with pytest.raises(DeviceError):
        device.apply(DeviceCommand("output", {"value": True}, 1, True))
    assert not transport.history


def test_file_names_are_case_insensitive_before_upload(model, monkeypatch):
    device, transport, _, _ = model
    monkeypatch.setattr(device, "file_catalog", lambda: {"USER.arb": 1024})
    with pytest.raises(DeviceError, match="overwrite"):
        device.apply(DeviceCommand("upload_waveform", {"name": "user", "samples": [0] * 256}, 2, True))
    assert not transport.history


def test_arb_sample_rate_does_not_invent_min_max_queries(model):
    device, transport, values, _ = model
    values.update(advanced=1, arb_enabled=1)
    command = DeviceCommand("arb_sample_rate", {"value": 1e-6}, 2)
    _, parameters = device.validate_command(command)
    device.preflight(command, parameters)
    assert not transport.history
    with pytest.raises(DeviceError):
        device.validate_command(DeviceCommand("arb_sample_rate", {"value": 1.25e9 + 1}, 2))
