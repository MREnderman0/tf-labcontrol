import sqlite3

import pytest

from lab_control.application.catalog import instrument_category, mode_applicable
from lab_control.domain.workspace import WORKSPACE_MODES
from lab_control.storage.repository import Repository
from lab_control.storage.workspace_migration import MIGRATION_KEY, migrate_general_workspace


def record(key, category, slots=None):
    return {
        "id": key,
        "adapterId": f"sim.{category}",
        "config": {"transport": "mock"},
        "candidates": {},
        "slots": slots or {},
    }


@pytest.mark.parametrize("previous", ["electronic", "optical"])
def test_migration_preserves_legacy_and_explicit_clears_and_uses_device_preferences(tmp_path, previous):
    repo = Repository(tmp_path)
    try:
        repo.save_workspace("current_mode", previous)
        devices = [
            ("osa", "optical_spectrometer", "optical"),
            ("rf", "spectrum_analyzer", "electronic"),
            ("scope", "oscilloscope", previous),
            ("source", "signal_generator", previous),
        ]
        for key, category, preferred in devices:
            repo.save(record(key, category))
            for mode in ("electronic", "optical"):
                repo.save_workspace(f"draft/{mode}/{key}/1/frequency", {"origin": mode})
                repo.save_workspace(
                    f"monitor-timing/{mode}/{key}", {"interval": 2 if mode == preferred else 9}
                )
            repo.save_workspace(f"draft/{preferred}/{key}/1/cleared", None)
            other = "electronic" if preferred == "optical" else "optical"
            repo.save_workspace(f"draft/{other}/{key}/1/cleared", {"value": 99})
            repo.save_workspace(f"draft/{other}/{key}/1/fallback", {"value": 7})
        repo.save_workspace("spectrum-markers/electronic/rf", [155])
        repo.save_workspace(f"scope-fine/{previous}/scope", False)
        repo.save_workspace("draft/general/source/1/frequency", {"value": 42})
        legacy = dict(repo.db.execute("SELECT id, document FROM workspace"))
        history = tmp_path / "old-record.json"
        history.write_bytes(b'{"mode":"optical","data":[1,2]}')
        report = migrate_general_workspace(repo, instrument_category)
        assert repo.load_workspace("current_mode") == "general"
        assert report["previous_mode"] == previous
        for key, _, preferred in devices:
            assert repo.load_workspace(f"draft/general/{key}/1/frequency") == (
                {"value": 42} if key == "source" else {"origin": preferred}
            )
            assert repo.load_workspace(f"draft/general/{key}/1/cleared", "missing") is None
            assert repo.load_workspace(f"draft/general/{key}/1/fallback") == {"value": 7}
            assert repo.load_workspace(f"monitor-timing/general/{key}") == {"interval": 2}
        assert repo.load_workspace("spectrum-markers/general/rf") == [155]
        assert repo.load_workspace("scope-fine/general/scope", True) is False
        for key, value in legacy.items():
            if key != "current_mode":
                assert (
                    repo.db.execute("SELECT document FROM workspace WHERE id=?", (key,)).fetchone()[0]
                    == value
                )
        repo.save_workspace("draft/general/osa/1/frequency", None)
        state = list(repo.db.execute("SELECT * FROM workspace ORDER BY id"))
        assert migrate_general_workspace(repo, instrument_category) == report
        assert list(repo.db.execute("SELECT * FROM workspace ORDER BY id")) == state
        assert history.read_bytes() == b'{"mode":"optical","data":[1,2]}'
    finally:
        repo.close()


def test_slots_and_dictionary_preferences_keep_conflicts_separate(tmp_path):
    repo = Repository(tmp_path)
    try:
        for row in [
            record("a", "signal_generator", {"electronic": "signal_generator:1"}),
            record("b", "signal_generator", {"optical": "signal_generator:1"}),
            record("c", "power_supply", {"electronic": "power_supply:1", "optical": ""}),
            record("meter", "optical_power_meter", {"optical": "optical_power_meter:1"}),
            record("osa", "optical_spectrometer", {"optical": "optical_spectrometer:2", "general": ""}),
        ]:
            repo.save(row)
        repo.save_workspace(
            "monitor_overview/all", {"slot:optical:optical_power_meter:1": {"enabled": False}}
        )
        repo.save_workspace("monitor_overview/electronic", {"slot:signal_generator:1": {"interval": 3}})
        repo.save_workspace("monitor_overview/optical", {"slot:signal_generator:1": {"interval": 8}})
        repo.save_workspace(
            "monitoring_channels/optical_power_meter", {"slot:optical:optical_power_meter:1": []}
        )
        before = {r["id"]: r["slots"] for r in repo.records()}
        report = migrate_general_workspace(repo, instrument_category)
        slots = {r["id"]: r["slots"] for r in repo.records()}
        assert set(report["slot_conflicts"]) == {"a", "b", "c"}
        assert all(slots[key]["general"] == "" for key in ("a", "b", "c", "osa"))
        assert slots["meter"]["general"] == "optical_power_meter:1"
        for key, legacy in before.items():
            assert all(slots[key][mode] == value for mode, value in legacy.items())
        saved = repo.load_workspace("monitor_overview/all")
        assert saved["device:a"] == {"interval": 3}
        assert saved["device:b"] == {"interval": 8}
        assert saved["slot:general:optical_power_meter:1"] == {"enabled": False}
        assert (
            repo.load_workspace("monitoring_channels/optical_power_meter")[
                "slot:general:optical_power_meter:1"
            ]
            == []
        )
    finally:
        repo.close()


def test_failed_migration_rolls_back_inventory_and_preferences(tmp_path):
    repo = Repository(tmp_path)
    try:
        repo.save(record("source", "signal_generator", {"electronic": "signal_generator:1"}))
        repo.save_workspace("current_mode", "optical")
        before = repo.records()
        with repo.db:
            repo.db.execute(
                f"CREATE TRIGGER fail_migration BEFORE INSERT ON workspace WHEN NEW.id = '{MIGRATION_KEY}' BEGIN SELECT RAISE(ABORT, 'fixture failure'); END"
            )
        with pytest.raises(sqlite3.IntegrityError):
            migrate_general_workspace(repo, instrument_category)
        assert repo.records() == before
        assert repo.load_workspace("current_mode") == "optical"
        assert repo.load_workspace(MIGRATION_KEY) is None
    finally:
        repo.close()


def test_existing_general_slot_and_cleared_preferences_win_over_legacy(tmp_path):
    repo = Repository(tmp_path)
    try:
        repo.save(record("owner", "signal_generator", {"general": "signal_generator:1"}))
        repo.save(record("legacy", "signal_generator", {"electronic": "signal_generator:1"}))
        repo.save(record("meter", "optical_power_meter", {"optical": "optical_power_meter:1"}))
        repo.save_workspace("draft/general/legacy/1/frequency", None)
        repo.save_workspace("draft/electronic/legacy/1/frequency", {"value": 100})
        repo.save_workspace(
            "monitor_overview/all",
            {
                "slot:general:optical_power_meter:1": {},
                "slot:optical:optical_power_meter:1": {"enabled": True},
            },
        )
        repo.save_workspace(
            "monitoring_channels/optical_power_meter",
            {
                "slot:general:optical_power_meter:1": [],
                "slot:optical:optical_power_meter:1": [1, 2],
            },
        )
        report = migrate_general_workspace(repo, instrument_category)
        slots = {r["id"]: r["slots"] for r in repo.records()}
        assert set(report["slot_conflicts"]) == {"legacy"}
        assert slots["owner"]["general"] == "signal_generator:1"
        assert slots["legacy"]["general"] == ""
        assert repo.load_workspace("draft/general/legacy/1/frequency", "missing") is None
        assert repo.load_workspace("monitor_overview/all")["slot:general:optical_power_meter:1"] == {}
        assert (
            repo.load_workspace("monitoring_channels/optical_power_meter")[
                "slot:general:optical_power_meter:1"
            ]
            == []
        )
    finally:
        repo.close()


def test_inconsistent_legacy_device_still_blocks_automatic_shared_slot_assignment(tmp_path):
    repo = Repository(tmp_path)
    try:
        repo.save(
            record(
                "ambiguous",
                "signal_generator",
                {
                    "electronic": "signal_generator:1",
                    "optical": "signal_generator:2",
                },
            )
        )
        repo.save(record("shared", "signal_generator", {"optical": "signal_generator:1"}))
        report = migrate_general_workspace(repo, instrument_category)
        assert set(report["slot_conflicts"]) == {"ambiguous", "shared"}
        assert all(row["slots"]["general"] == "" for row in repo.records())
    finally:
        repo.close()


@pytest.mark.parametrize("mode", WORKSPACE_MODES)
def test_known_device_categories_are_available_in_all_accepted_workspaces(mode):
    for category in (
        "optical_power_meter",
        "optical_spectrometer",
        "spectrum_analyzer",
        "oscilloscope",
        "power_supply",
        "signal_generator",
    ):
        assert mode_applicable(record(category, category), mode)
    assert not mode_applicable({"adapterId": "removed.model"}, mode)
    assert not mode_applicable(record("source", "signal_generator"), "invalid")
