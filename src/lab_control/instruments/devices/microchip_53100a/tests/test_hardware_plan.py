from copy import deepcopy

import pytest

from lab_control.domain.instruments import ApplyResult, DeviceError, DeviceStatus, ParameterReading
from lab_control.instruments.devices.microchip_53100a.hardware.readonly import validate_settings


def run_plan(failure=""):
    original = {
        "input_frequency": {"automatic": True, "value": None},
        "reference_frequency": {"automatic": False, "value": 10e6},
    }
    state = deepcopy(original)
    commands = []
    lost = False
    report = {"stages": {}}

    def call(operation, argument=None):
        nonlocal lost
        if lost:
            raise DeviceError("Previous communication timeout", "disconnected")
        if operation == "read_settings":
            return tuple(
                ParameterReading(key, 1, deepcopy(value), "service_readback") for key, value in state.items()
            )
        if operation == "status":
            return DeviceStatus({"state": "Ready\nTime Constant: Infinite"})
        commands.append(argument)
        if failure == "timeout":
            lost = True
            return ApplyResult("uncertain", argument.operation, argument.parameters, error="timeout")
        value = {
            "automatic": argument.parameters.get("automatic", False),
            "value": argument.parameters.get("value"),
        }
        state[argument.operation] = value
        if failure == "restore" and value["automatic"]:
            return ApplyResult("uncertain", argument.operation, argument.parameters, error="restore failure")
        if failure == "mismatch" and len(commands) == 1:
            state[argument.operation]["value"] = 30e6
            return ApplyResult(
                "uncertain", argument.operation, argument.parameters, error="readback mismatch"
            )
        return ApplyResult("success", argument.operation, argument.parameters, deepcopy(value))

    error = None
    try:
        validate_settings(call, report, lambda: None)
    except Exception as exc:
        error = exc
    return report, commands, state, original, error


def test_plan_uses_alternate_frequency_and_restores_auto_and_other_input():
    report, commands, state, original, error = run_plan()
    assert error is None and state == original
    assert [c.parameters for c in commands] == [
        {"value": 10e6},
        {"automatic": True},
        {"value": 20e6},
        {"value": 10e6},
    ]
    assert report["stages"]["restoration"] == "passed"


@pytest.mark.parametrize("failure", ["timeout", "mismatch", "restore"])
def test_first_failure_stops_later_test_writes(failure):
    report, commands, _, _, error = run_plan(failure)
    assert error is not None
    assert all(c.operation == "input_frequency" for c in commands)
    assert sum(c.parameters == {"value": 10e6} for c in commands) == 1
    if failure == "timeout":
        assert len(commands) == 1
    if failure != "mismatch":
        assert report["stages"]["restoration"]["state"] == "failed"
