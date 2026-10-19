from __future__ import annotations

import math

import pytest

from lab_control.domain.instruments import ValidationError
from lab_control.domain.spectrum import prepare_optical_changes, wavelength_draft
from lab_control.instruments.devices.ceyear_6362d import DEFINITION
from lab_control.instruments.devices.ceyear_6362d.tests.test_contract import adapter


def caps():
    return adapter().capabilities()


def test_wavelength_draft_clamps_only_the_edited_endpoint():
    assert wavelength_draft({"start_wavelength": 500, "stop_wavelength": 1800}, "start_wavelength", 600, 1700) == {
        "start_wavelength": 600,
        "stop_wavelength": 1700,
    }
    assert wavelength_draft({"start_wavelength": 1650, "stop_wavelength": 1600}, "stop_wavelength", 600, 1700) == {
        "start_wavelength": 1650,
        "stop_wavelength": 1650,
    }


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_wavelength_draft_is_rejected(value):
    with pytest.raises(ValidationError):
        wavelength_draft({"start_wavelength": value, "stop_wavelength": 1600}, "start_wavelength", 600, 1700)


@pytest.mark.parametrize("value", [0.03, 2.1, math.nan])
def test_rbw_discrete_and_finite_validation(value):
    current = {"start_wavelength": 1540, "stop_wavelength": 1560, "rbw": 0.1, "sensitivity": "NORMAL"}
    target = {**current, "rbw": value}
    with pytest.raises(ValidationError):
        prepare_optical_changes(current, target, caps())


@pytest.mark.parametrize("value", ["LOW", "", None])
def test_sensitivity_enum_validation(value):
    current = {"start_wavelength": 1540, "stop_wavelength": 1560, "rbw": 0.1, "sensitivity": "NORMAL"}
    target = {**current, "sensitivity": value}
    with pytest.raises(ValidationError):
        prepare_optical_changes(current, target, caps())


def test_endpoint_writes_are_ordered_to_keep_intermediate_range_legal():
    current = {"start_wavelength": 1540, "stop_wavelength": 1560, "rbw": 0.1, "sensitivity": "NORMAL"}
    target = {**current, "start_wavelength": 1580, "stop_wavelength": 1600}
    assert [command.operation for command in prepare_optical_changes(current, target, caps())] == [
        "stop_wavelength",
        "start_wavelength",
    ]


def test_invalid_endpoint_order_has_zero_commands():
    current = {"start_wavelength": 1540, "stop_wavelength": 1560, "rbw": 0.1, "sensitivity": "NORMAL"}
    target = {**current, "start_wavelength": 1600, "stop_wavelength": 1500}
    with pytest.raises(ValidationError):
        prepare_optical_changes(current, target, caps())
