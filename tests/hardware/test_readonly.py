"""Retired entry point: hardware checks belong to the model's recorded workflow."""

import pytest


@pytest.mark.hardware
def test_configured_instrument_readonly():
    pytest.skip(
        "Legacy hardware pytest entry retired; use the selected model's hardware entry point "
        "and docs/testing/records.md. Environment variables never enable hardware in this test."
    )
