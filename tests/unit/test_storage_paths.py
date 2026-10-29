import json
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from lab_control.domain.instruments import DeviceIdentity, ValidationError
from lab_control.storage.paths import BatchSaveContext, SaveContext, filename_part
from lab_control.storage.recording import Recording
from lab_control.storage.repository import Repository

START = datetime(2026, 9, 27, 23, 59, 59, 123456, tzinfo=timezone(timedelta(hours=8)))


@pytest.mark.parametrize("mode", ["general", "optical"])
def test_save_context_freezes_current_slot_and_does_not_revive_cleared_legacy_binding(tmp_path, mode):
    repo = Repository(tmp_path)
    record = {
        "id": "fixture",
        "candidates": {},
        "slots": {"general": "optical_power_meter:1", "optical": "optical_power_meter:2"},
    }
    try:
        repo.save(record)
        context = repo.new_save_context(mode, "single_frame", device_id="fixture")
        record["slots"]["general"] = ""
        repo.save(record)
        unbound = repo.new_save_context(mode, "single_frame", device_id="fixture")
        assert not unbound.slot and not unbound.slot_label
        assert "功率计" not in unbound.folder.name
        assert context.slot == "optical_power_meter:1" and "功率计1" in context.folder.name
        folder = context.prepare()
        saved = json.loads((folder / "_raw" / "recording.json").read_text(encoding="utf-8"))
        assert saved["slot"] == context.slot and saved["slot_label"] == "功率计1"
    finally:
        repo.close()


@pytest.mark.parametrize("kind", ["continuous", "single_frame"])
def test_local_start_date_names_and_original_metadata_are_fixed(tmp_path, kind):
    identity = DeviceIdentity("Fixture", "Model/Pro", "SN:1/?", "1.0")
    context = SaveContext(tmp_path / "数据 中文", "optical", kind, "stable-id", identity, started_at=START)
    assert not context.root.exists()
    assert context.folder.parent.name == "2026-09-27"
    assert context.folder.name.startswith(f"23-59-59-123_Model_Pro_SN_1___{kind}_")
    with pytest.raises(FrozenInstanceError):
        context.mode = "electronic"
    folder = context.prepare()
    metadata = json.loads((folder / "_raw" / "recording.json").read_text(encoding="utf-8"))
    assert metadata["identity"]["serial"] == "SN:1/?"
    assert metadata["identity"]["model"] == "Model/Pro"
    assert metadata["started_local"] == START.isoformat()
    assert metadata["started_utc"] == "2026-09-27T15:59:59.123456+00:00"
    assert metadata["mode"] == "optical" and metadata["device_id"] == "stable-id"
    with pytest.raises(FileExistsError):
        context.prepare()


@pytest.mark.parametrize(
    "value,expected",
    [
        ("CON", "_CON"),
        ("LPT1.txt", "_LPT1.txt"),
        ("a/b:c?d*", "a_b_c_d_"),
        (" . ", "unknown"),
        ("abc. ", "abc"),
        ("COM¹", "_COM¹"),
        ("x" * 100, "x" * 48),
    ],
)
def test_windows_names_are_safe(value, expected):
    assert filename_part(value) == expected


def test_missing_identity_fallback_and_same_millisecond_never_reuses_folder(tmp_path):
    first = SaveContext(
        tmp_path, "electronic", "continuous", "device-1", adapter_id="fixture.model", started_at=START
    )
    second = replace(first, run_id="another-run")
    assert "fixture.model_device-1_continuous" in first.folder.name
    assert first.folder != second.folder and first.folder.parent == second.folder.parent
    first.prepare()
    second.prepare()


def test_batch_context_groups_devices_and_does_not_write_until_start(tmp_path):
    batch = BatchSaveContext(tmp_path / "chosen", started_at=START)
    first = SaveContext(tmp_path, "electronic", "continuous", "one", batch=batch)
    second = SaveContext(tmp_path, "optical", "single_frame", "two", batch=batch)
    assert first.root == second.root == batch.root
    assert first.started_at == second.started_at == START
    assert first.folder.parent == second.folder.parent == batch.folder
    assert batch.folder.parent.parent.name == "批量采集" and not batch.folder.exists()
    assert first.prepare() != second.prepare()


def test_same_model_devices_and_same_time_exports_keep_distinct_data(tmp_path):
    repo = Repository(tmp_path)
    try:
        contexts = [
            repo.new_save_context(
                "electronic",
                "single_frame",
                device_id=f"device-{serial}",
                identity=DeviceIdentity("Fixture", "Identical", serial),
            )
            for serial in ("SN1", "SN2", "")
        ]
        folders = [replace(context, started_at=START).prepare() for context in contexts]
        assert len(set(folders)) == 3
        assert all(folder.parent == folders[0].parent for folder in folders)
        assert "Identical_device-" in folders[2].name
    finally:
        repo.close()


def test_root_setting_persists_without_moving_inventory_or_logs(tmp_path):
    application = tmp_path / "application"
    chosen = tmp_path / "采集数据 中文"
    repo = Repository(application)
    try:
        repo.save_workspace("unrelated", "retained")
        repo.set_data_root(chosen)
        repo.audit("fixture")
        assert repo.data_root == chosen and repo.root == application
        assert (application / "inventory.sqlite3").exists() and (application / "logs").exists()
        assert list(chosen.iterdir()) == []
    finally:
        repo.close()
    repo = Repository(application)
    try:
        assert repo.data_root == chosen and repo.load_workspace("unrelated") == "retained"
        bad = tmp_path / "file"
        bad.write_text("keep", encoding="utf-8")
        with pytest.raises(OSError):
            repo.set_data_root(bad)
        assert repo.data_root == chosen
        with pytest.raises(ValidationError):
            repo.set_data_root(type(chosen)("relative"))
        assert repo.data_root == chosen
    finally:
        repo.close()


def test_mock_custom_root_and_preferences_are_isolated(tmp_path):
    real, mock = Repository(tmp_path / "real"), Repository(tmp_path / "mock", simulation=True)
    chosen = tmp_path / "shared-choice"
    try:
        mock.set_data_root(mock.data_root_base)
        assert mock.data_root == tmp_path / "mock" / "data"
        real.set_data_root(chosen)
        assert mock.data_root != chosen
        mock.set_data_root(chosen)
        assert real.data_root == chosen and mock.data_root == chosen / "simulation"
    finally:
        real.close()
        mock.close()


def test_unwritable_root_does_not_replace_saved_setting(tmp_path, monkeypatch):
    repo = Repository(tmp_path / "app")
    before = repo.data_root

    def denied(**kwargs):
        raise PermissionError("fixture permission denied")

    monkeypatch.setattr("lab_control.storage.repository.TemporaryFile", denied)
    try:
        with pytest.raises(PermissionError):
            repo.set_data_root(tmp_path / "denied")
        assert repo.data_root == before
    finally:
        repo.close()


def test_empty_continuous_recording_keeps_manifest_and_zero_summary(tmp_path):
    context = SaveContext(tmp_path, "electronic", "continuous", "fixture", started_at=START)
    recording = Recording(tmp_path, "electronic", context=context)
    assert (context.folder / "_raw" / "recording.json").is_file()
    assert recording.finish().result(5) == []
    summary = json.loads((context.folder / "_raw" / "summary.json").read_text(encoding="utf-8"))
    assert summary["received"] == summary["committed"] == 0
    assert summary["started_local"] == START.isoformat()


def test_context_validation_rejects_mismatched_operation_and_naive_time(tmp_path):
    with pytest.raises(ValidationError):
        SaveContext(tmp_path, "electronic", "continuous", started_at=datetime(2026, 9, 27))
    context = SaveContext(tmp_path, "electronic", "single_frame")
    with pytest.raises(ValidationError):
        Recording(tmp_path, "electronic", context=context)
    assert not context.folder.exists()
