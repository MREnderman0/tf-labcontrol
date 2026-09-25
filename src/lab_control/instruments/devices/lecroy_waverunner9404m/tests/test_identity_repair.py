from copy import deepcopy
from dataclasses import replace

import pytest

from lab_control.domain.instruments import DeviceError, DeviceIdentity, jsonable

from ..hardware.repair_identity import plan


def records():
    good = DeviceIdentity(
        "LECROY", "WAVERUNNER9404M", "FIXTURE", "9.2.0", raw="*IDN LECROY,WAVERUNNER9404M,FIXTURE,9.2.0"
    )
    result = []
    resource = "TCPIP0::192.0.2.1::inst0::INSTR"
    for index, maker in enumerate(("LECROY", "*IDN LECROY")):
        identity = replace(good, manufacturer=maker)
        result.append(
            dict(
                id=str(index),
                identity=jsonable(identity),
                identityKey=identity.key,
                adapterId="lecroy.9404m",
                resources=[resource],
                displayName="",
                slots={},
                config=dict(adapter="lecroy.9404m", resource=resource, expected_serial="FIXTURE", timeout=10),
                candidates={resource: {"verifiedIdentityKey": identity.key, "retired": index == 0}},
            )
        )
    result[-1]["identityConflict"] = jsonable(good)
    result[-1]["displayName"] = "用户名称"
    result[-1]["slots"] = {"electronic": "oscilloscope:1"}
    return result, good


def test_repair_keeps_original_id_preferences_and_does_not_mutate_evidence():
    saved, identity = records()
    before = deepcopy(saved)
    repaired, removed = plan(saved, identity)
    assert repaired["id"] == "0" and removed == ("1",)
    assert repaired["config"] == saved[0]["config"] and repaired["displayName"] == "用户名称"
    assert repaired["slots"] == saved[1]["slots"] and not repaired.get("identityConflict")
    assert repaired["identityKey"] == identity.key and saved == before
    assert not next(iter(repaired["candidates"].values()))["retired"]


@pytest.mark.parametrize(
    "fault", ["serial", "firmware", "raw", "conflict", "settings", "name", "slot", "address"]
)
def test_real_identity_or_user_preference_conflicts_are_not_erased(fault):
    saved, identity = records()
    if fault in {"serial", "firmware", "raw"}:
        saved[1]["identity"][fault] = "DIFFERENT"
    elif fault == "conflict":
        saved[1]["identityConflict"] = {"reason": "same_identity_at_multiple_network_hosts"}
    elif fault == "settings":
        saved[1]["config"]["timeout"] = 15
    elif fault == "name":
        saved[0]["displayName"] = "另一个名称"
    elif fault == "slot":
        saved[0]["slots"] = {"electronic": "oscilloscope:2"}
    else:
        saved[1]["resources"].append("TCPIP0::192.0.2.2::inst0::INSTR")
    with pytest.raises(DeviceError):
        plan(saved, identity)
