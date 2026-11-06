"""Sensor reference equations, asset binding and raw-resistance preservation."""

import math
from copy import deepcopy
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from lab_control.domain.instruments import AcquisitionRequest, DeviceError
from lab_control.storage.recording import SegmentWriter, read_frames

from ..thermistor_curves import BANDS, MF501, S02, temperature
from .test_connection_setup import CHANNELS, bound_adapter


@pytest.mark.parametrize("celsius", [-30, -20, 0, 25, 40, 60])
def test_mf501_inverse_matches_independent_forward_equation(celsius):
    with localcontext() as ctx:
        ctx.prec = 45
        kelvin = Decimal(celsius) + Decimal("273.15")
        resistance = (
            Decimal("-6.01188") + Decimal("4622.53337") / kelvin - Decimal("86421.72414") / kelvin**2
        ).exp()
    assert temperature(float(resistance), MF501) == pytest.approx(celsius, abs=1e-9)
    assert temperature(float(resistance), MF501, "F") == pytest.approx(celsius * 1.8 + 32, abs=1e-9)
    assert temperature(float(resistance), MF501, "K") == pytest.approx(celsius + 273.15, abs=1e-9)


def test_workbook_cached_example():
    assert temperature(9992, S02) == pytest.approx(25.018175659806786, abs=1e-10)
    assert temperature(10000, S02) == pytest.approx(24.99994974682602, abs=1e-10)


@pytest.mark.parametrize("boundary", [681.6, 3599, 32770])
@pytest.mark.parametrize("side", [-1, 0, 1])
def test_workbook_lower_bound_lookup(boundary, side):
    resistance = boundary + side * 1e-6
    row = max((b for b in BANDS if b[0] <= resistance), key=lambda b: b[0])
    with localcontext() as ctx:
        ctx.prec = 45
        x = (Decimal(str(resistance)) / Decimal(10000)).ln()
        a, b, c, d = map(lambda v: Decimal(str(v)), row[1:])
        expected = 1 / (a + b * x + c * x * x + d * x * x * x) - Decimal("273.15")
    assert temperature(resistance, S02) == pytest.approx(float(expected), abs=1e-9)


@pytest.mark.parametrize("curve", [MF501, S02])
@pytest.mark.parametrize("resistance", [0, -1, math.nan, math.inf, 9.9e37, 1e-10, 1e20])
def test_invalid_or_outside_document_range(curve, resistance):
    with pytest.raises(DeviceError):
        temperature(resistance, curve)


def test_full_bank_is_converted_and_resistance_survives_storage(tmp_path):
    adapter, transport, stored = bound_adapter()
    adapter.remember_resistance_temperature_setup(CHANNELS, confirmed=True)
    adapter.prepare_acquisition()
    batch = adapter.acquire(AcquisitionRequest("scalar", tuple(CHANNELS), confirmed=True))
    writer = SegmentWriter(tmp_path)
    writer.append(1, batch)
    writer.close()
    saved = list(read_frames(Path(writer.paths[0])))[0]
    assert len(saved["measurements"]) == 19
    for m in saved["measurements"]:
        ch = m["channel"]
        provenance = m["metadata"]["temperature_conversion"]
        resistance = (5000 if ch <= 416 else 10000) + ch / 100
        assert provenance["resistance_ohm"] == resistance
        assert provenance["curve_id"] == CHANNELS[ch] and provenance["version"] == 1
        assert m["values"][0] == pytest.approx(temperature(resistance, CHANNELS[ch]), abs=1e-9)
        assert (m["quantity"], m["unit"]) == ("temperature", "°C")
    assert all('"TEMP"' not in command for command in transport.state.writes)


def test_old_temperature_profile_never_restores_native_mode():
    adapter, transport, stored = bound_adapter()
    adapter.remember_resistance_temperature_setup(CHANNELS, confirmed=True)
    old = deepcopy(stored["value"])
    old.update(schema_version=1, channels={str(ch): 5000 for ch in CHANNELS})
    adapter.connection_setup.save(old)
    with pytest.raises(DeviceError, match="已停用"):
        adapter.prepare_acquisition()
    with pytest.raises(DeviceError, match="已停用"):
        adapter.connection_setup.conversion()
    assert not transport.state.writes and "READ?" not in transport.history


def test_missing_channel_binding_is_rejected_before_scan():
    adapter, transport, stored = bound_adapter()
    adapter.remember_resistance_temperature_setup(CHANNELS, confirmed=True)
    adapter.prepare_acquisition()
    stored["value"]["channels"].pop("419")
    with pytest.raises(DeviceError, match="不匹配"):
        adapter.acquire(AcquisitionRequest("scalar", tuple(CHANNELS), confirmed=True))
    assert "READ?" not in transport.history
