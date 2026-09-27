import sqlite3
from dataclasses import replace

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.demo.instrument import SimulatedInstrument
from lab_control.discovery.service import DiscoveryCandidate, Inventory
from lab_control.domain.instruments import DeviceError


@pytest.fixture
def workspace(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        ids = []
        for name in ("first", "second", "third"):
            config = ConnectionConfig(f"mock://{name}", "rs.sma100b", "mock", expected_serial=name)
            key = runtime.add(config)
            runtime.connect(key).result(3)
            ids.append(key)
        yield runtime, ids


def slots(runtime):
    return {r["id"]: r["slots"] for r in runtime.inventory.all()}


def test_confirmed_replacement_is_persistent_and_keeps_both_sessions_and_other_mode(workspace):
    runtime, (first, second, _) = workspace
    runtime.bind_slot(first, "signal_generator:1", "electronic")
    runtime.bind_slot(second, "signal_generator:2", "electronic")
    runtime.bind_slot(first, "signal_generator:1", "optical")
    sessions = dict(runtime.sessions.sessions)
    with pytest.raises(DeviceError) as error:
        runtime.bind_slot(second, "signal_generator:1", "electronic")
    assert error.value.code == "slot_conflict"
    runtime.bind_slot(second, "signal_generator:1", "electronic", replace_device_id=first)
    expected = slots(runtime)
    assert expected[first] == {"electronic": "", "optical": "signal_generator:1"}
    assert expected[second] == {"electronic": "signal_generator:1"}
    assert runtime.sessions.sessions == sessions
    assert {r["id"]: r["slots"] for r in Inventory(runtime.repository).all()} == expected
    runtime.sessions.disconnect(first)
    runtime.connect(first).result(3)
    assert slots(runtime) == expected


@pytest.mark.parametrize("participant", [0, 1])
@pytest.mark.parametrize(
    "flag,value",
    [
        ("pending", 1),
        ("state", "busy"),
        ("state", "connecting"),
        ("monitoring", True),
        ("recording", True),
        ("control_pending", 1),
        ("control_group", True),
        ("closing", True),
        ("active_task", "continuous_acquisition"),
    ],
)
def test_replacement_checks_both_tasks_including_recording_drain(workspace, participant, flag, value):
    runtime, ids = workspace
    first, second = ids[:2]
    runtime.bind_slot(first, "signal_generator:1", "electronic")
    previous = slots(runtime)
    session = runtime.sessions.sessions[ids[participant]]
    original = getattr(session, flag)
    setattr(session, flag, value)
    try:
        with pytest.raises(DeviceError) as error:
            runtime.bind_slot(second, "signal_generator:1", "electronic", replace_device_id=first)
        assert error.value.code == "task_busy" and slots(runtime) == previous
    finally:
        setattr(session, flag, original)


def test_changed_occupant_or_freed_slot_requires_new_confirmation(workspace):
    runtime, (first, second, third) = workspace
    runtime.bind_slot(first, "signal_generator:1", "electronic")
    runtime.bind_slot(third, "signal_generator:1", "electronic", replace_device_id=first)
    for release in (False, True):
        if release:
            runtime.bind_slot(third, "", "electronic")
        previous = slots(runtime)
        with pytest.raises(DeviceError) as error:
            runtime.bind_slot(second, "signal_generator:1", "electronic", replace_device_id=first)
        assert error.value.code == "slot_changed" and slots(runtime) == previous


def test_failed_second_database_write_rolls_back_database_and_memory(workspace):
    runtime, (first, second, _) = workspace
    runtime.bind_slot(first, "signal_generator:1", "electronic")
    runtime.bind_slot(second, "signal_generator:2", "electronic")
    before = runtime.inventory.all()
    persisted_before = runtime.repository.records()
    with runtime.repository.db:
        runtime.repository.db.execute(
            f"CREATE TRIGGER fail_slot BEFORE INSERT ON inventory WHEN NEW.id = '{first}' "
            "BEGIN SELECT RAISE(ABORT, 'fixture write failure'); END"
        )
    try:
        with pytest.raises(sqlite3.IntegrityError, match="fixture write failure"):
            runtime.bind_slot(second, "signal_generator:1", "electronic", replace_device_id=first)
        assert runtime.inventory.all() == before
        assert runtime.repository.records() == persisted_before
    finally:
        with runtime.repository.db:
            runtime.repository.db.execute("DROP TRIGGER fail_slot")


def test_restart_and_exact_identity_at_new_address_reuse_slot_but_same_model_does_not(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        config = ConnectionConfig("mock://old", "rs.sma100b", "mock", expected_serial="SAME-ASSET")
        key = runtime.add(config)
        runtime.connect(key).result(3)
        runtime.bind_slot(key, "signal_generator:1", "electronic")
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        moved = replace(config, resource="mock://new-address")
        identity = SimulatedInstrument(moved).identify()
        merged = runtime.inventory.merge(
            DiscoveryCandidate(moved, "configured"), "scan", identity, moved.adapter
        )
        assert merged["id"] == key and merged["slots"]["electronic"] == "signal_generator:1"
        another = replace(config, resource="mock://another", expected_serial="ANOTHER-ASSET")
        record = runtime.inventory.merge(
            DiscoveryCandidate(another, "configured"),
            "scan",
            SimulatedInstrument(another).identify(),
            another.adapter,
        )
        assert record["id"] != key and record["slots"] == {}
        runtime.bind_slot(key, "", "electronic")
        runtime.inventory.merge(DiscoveryCandidate(moved, "configured"), "scan2", identity, moved.adapter)
        assert slots(runtime)[key]["electronic"] == ""


@pytest.mark.parametrize("historical,placeholder", [("", "signal_generator:1"), ("signal_generator:1", "")])
def test_alias_merge_preserves_explicit_unbinding_and_marks_conflicting_choices(
    workspace, historical, placeholder
):
    runtime, (key, _, _) = workspace
    runtime.bind_slot(key, historical, "electronic")
    original = next(r for r in runtime.inventory.all() if r["id"] == key)
    config = replace(ConnectionConfig(**original["config"]), resource="mock://alias")
    candidate = DiscoveryCandidate(config, "configured")
    extra = runtime.inventory.merge(candidate, "scan")
    runtime.inventory.edit(extra["id"], slot=placeholder)
    identity = SimulatedInstrument(config).identify()
    runtime.inventory.merge(candidate, "scan", identity, config.adapter)
    assert slots(runtime)[key]["electronic"] == historical
    assert slots(runtime)[extra["id"]]["electronic"] == placeholder
    assert next(r for r in runtime.inventory.all() if r["id"] == extra["id"])["discoveryState"] == "conflict"
