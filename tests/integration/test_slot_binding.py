import sqlite3
from dataclasses import replace

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.demo.instrument import SimulatedInstrument
from lab_control.discovery.service import DiscoveryCandidate, Inventory
from lab_control.domain.instruments import DeviceError, ValidationError


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
    runtime.inventory.edit(extra["id"], slot=placeholder, mode="electronic")
    identity = SimulatedInstrument(config).identify()
    runtime.inventory.merge(candidate, "scan", identity, config.adapter)
    assert slots(runtime)[key]["electronic"] == historical
    assert slots(runtime)[extra["id"]]["electronic"] == placeholder
    assert next(r for r in runtime.inventory.all() if r["id"] == extra["id"])["discoveryState"] == "conflict"


def test_custom_slots_persist_and_saved_labels_survive_deletion(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        key = runtime.add(ConnectionConfig("mock://named", "rs.sma100b", "mock"))
        slot = runtime.create_slot("signal_generator", "  冷却光 RF  ")
        assert runtime.create_slot("signal_generator", "冷却光 rf") == slot
        runtime.bind_slot(key, slot, "general")
        context = runtime.repository.new_save_context("general", "single_frame", device_id=key)
        assert context.slot == slot and context.slot_label == "冷却光 RF"
        # Category and mode boundaries still apply to user-created slots.
        with pytest.raises(ValidationError):
            runtime.bind_slot(key, slot, "optical")
        wrong = runtime.create_slot("power_supply", "冷却光 RF")
        with pytest.raises(ValidationError):
            runtime.bind_slot(key, wrong, "general")
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        assert slots(runtime)[key]["general"] == slot
        assert runtime.repository.function_slots.label(slot) == "冷却光 RF"
        with pytest.raises(DeviceError, match="先解除绑定"):
            runtime.delete_slot(slot)
        runtime.bind_slot(key, "", "general")
        runtime.delete_slot(slot)
        recreated = runtime.create_slot("signal_generator", "冷却光 RF")
        assert recreated != slot  # Deleted-slot preferences must not attach to a new slot.
        assert context.slot_label == "冷却光 RF"
        with pytest.raises(ValidationError):
            runtime.bind_slot(key, slot, "general")


def test_deleting_all_default_slots_persists_without_reseeding_or_changing_other_modes(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        for slot in runtime.repository.function_slots.definitions("signal_generator"):
            runtime.delete_slot(slot)
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        assert runtime.repository.function_slots.definitions("signal_generator") == {}
        assert len(runtime.repository.function_slots.definitions("signal_generator", "electronic")) == 2
        assert len(runtime.repository.function_slots.definitions("optical_power_meter")) == 4
        key = runtime.add(ConnectionConfig("mock://empty", "rs.sma100b", "mock"))
        with pytest.raises(ValidationError):
            runtime.bind_slot(key, "signal_generator:1", "general")
        assert runtime.create_slot("signal_generator", "新槽位")


@pytest.mark.parametrize("name", ["", "   ", "x" * 65, "a\nb", "a\x00b", "a\x7fb"])
def test_invalid_slot_names_do_not_change_definitions(workspace, name):
    runtime, _ = workspace
    before = runtime.repository.function_slots.definitions("signal_generator")
    with pytest.raises(ValidationError):
        runtime.create_slot("signal_generator", name)
    assert runtime.repository.function_slots.definitions("signal_generator") == before


def test_custom_slot_replacement_keeps_busy_and_occupancy_guards(workspace):
    runtime, (first, second, _) = workspace
    slot = runtime.create_slot("signal_generator", "实验光路")
    runtime.bind_slot(first, slot, "general")
    with pytest.raises(DeviceError, match="先解除绑定"):
        runtime.delete_slot(slot)
    runtime.sessions.sessions[first].recording = True
    try:
        with pytest.raises(DeviceError) as error:
            runtime.bind_slot(second, slot, "general", replace_device_id=first)
        assert error.value.code == "task_busy"
    finally:
        runtime.sessions.sessions[first].recording = False
    runtime.bind_slot(second, slot, "general", replace_device_id=first)
    assert slots(runtime)[first]["general"] == ""
    assert slots(runtime)[second]["general"] == slot


@pytest.mark.parametrize("action", ["create", "delete"])
def test_slot_catalog_write_failure_keeps_previous_definitions(workspace, action):
    runtime, _ = workspace
    before = runtime.repository.function_slots.definitions("signal_generator")
    with runtime.repository.db:
        runtime.repository.db.execute(
            "CREATE TRIGGER fail_slot_catalog BEFORE INSERT ON workspace "
            "WHEN NEW.id LIKE 'function_slots/%' "
            "BEGIN SELECT RAISE(ABORT, 'fixture write failure'); END"
        )
    try:
        with pytest.raises(sqlite3.IntegrityError):
            if action == "create":
                runtime.create_slot("signal_generator", "新槽位")
            else:
                runtime.delete_slot("signal_generator:1")
        assert runtime.repository.function_slots.definitions("signal_generator") == before
    finally:
        with runtime.repository.db:
            runtime.repository.db.execute("DROP TRIGGER fail_slot_catalog")
