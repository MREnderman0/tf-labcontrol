"""Opt-in: LAB_CONTROL_HARDWARE_CONFIG and LAB_CONTROL_HARDWARE_RESOURCE are both required."""

import os
from pathlib import Path

import pytest

from lab_control.application.config import load_config
from lab_control.application.runtime import ApplicationRuntime


@pytest.mark.hardware
def test_configured_instrument_readonly(tmp_path):
    config_path, resource = (
        os.getenv("LAB_CONTROL_HARDWARE_CONFIG"),
        os.getenv("LAB_CONTROL_HARDWARE_RESOURCE"),
    )
    if not config_path or not resource:
        pytest.skip("Explicit hardware configuration and resource required")
    configs = tuple(config for config in load_config(Path(config_path)) if config.resource == resource)
    assert len(configs) == 1, "Exactly one configured target must match"
    with ApplicationRuntime(configs, root=tmp_path) as app:
        device_id = app.add(configs[0])
        identity = app.connect(device_id).result()
        assert identity.model
        assert app.sessions.submit(device_id, "capabilities").result().operations
        app.sessions.submit(device_id, "status").result()
