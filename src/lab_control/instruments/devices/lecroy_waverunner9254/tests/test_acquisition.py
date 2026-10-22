import json
from pathlib import Path

import numpy as np
import pytest

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceError, DeviceIdentity, Unsupported
from lab_control.instruments.registry import PROFILES, build_scpi_adapter
from lab_control.transport.base import MockTransport

CASE = json.loads((Path(__file__).parent / "fixtures/acquisition.json").read_text(encoding="utf-8"))


def make_device(invalid=False):
    exchanges = [
        (command, bytes.fromhex(reply["hex"]) if isinstance(reply, dict) else reply.encode())
        for command, reply in CASE["exchanges"]
    ]
    if invalid:
        exchanges = [
            (command, b"malformed" if command == CASE["bad_query"] else reply) for command, reply in exchanges
        ]
    wire = MockTransport(exchanges)
    wire.open()
    device = build_scpi_adapter(ConnectionConfig("mock://protocol", "lecroy.9254"), wire)
    profile = PROFILES["lecroy.9254"]
    device.identity = DeviceIdentity(profile.manufacturer[0], profile.models[0], "SYNTHETIC")
    return device, wire


def test_acquisition_transcript_scaling_and_units():
    device, wire = make_device()
    batch = device.acquire(
        AcquisitionRequest(CASE["kind"], (1,), CASE["points"], confirmed=CASE["confirmed"])
    )
    measurement = batch.measurements[0]
    expected = [complex(*v) if isinstance(v, list) else v for v in CASE["values"]]
    np.testing.assert_allclose(measurement.values, expected)
    assert measurement.unit == CASE["unit"]
    if CASE["axis"] is not None:
        np.testing.assert_allclose(measurement.axis, CASE["axis"])
    assert not wire.exchanges
    assert wire.history == [e[0] for e in CASE["exchanges"]]


def test_invalid_frame_is_rejected_without_command_replay():
    if CASE["bad_query"] is None:
        pytest.skip("No raw-frame parser for this scalar route")
    device, wire = make_device(invalid=True)
    with pytest.raises(DeviceError):
        device.acquire(AcquisitionRequest(CASE["kind"], (1,), CASE["points"], confirmed=CASE["confirmed"]))
    assert wire.history.count(CASE["bad_query"]) == 1


def test_wrong_acquisition_kind_does_not_touch_transport():
    device, wire = make_device()
    with pytest.raises(Unsupported):
        device.acquire(AcquisitionRequest("status", (1,), 1))
    assert not wire.history
