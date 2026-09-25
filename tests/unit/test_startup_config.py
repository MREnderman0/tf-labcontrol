from pathlib import Path

import pytest

from lab_control.application.config import load_startup_config
from lab_control.discovery.service import ConfiguredDiscoveryProvider
from lab_control.domain.instruments import ValidationError


def manifest(path: Path, resource: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '[[devices]]\nresource = "' + resource + '"\nadapter = "test.model"\nexpected_serial = "TEST"\n',
        encoding="utf-8",
    )
    return path


def test_default_manifest_supplies_explicit_network_scan_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_CONTROL_HOME", str(tmp_path))
    resource = "TCPIP0::instrument.example.invalid::inst0::INSTR"
    manifest(tmp_path / "config/devices.toml", resource)
    config = load_startup_config()
    candidates = ConfiguredDiscoveryProvider(config).enumerate()
    assert len(candidates) == 1
    assert candidates[0].config.resource == resource
    assert candidates[0].config.expected_serial == "TEST"


def test_no_default_does_not_scan_backups_or_examples(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_CONTROL_HOME", str(tmp_path))
    manifest(tmp_path / "config/device.tcp-backup.toml", "tcp://backup.example.invalid:5025")
    assert load_startup_config() == ()


def test_explicit_path_overrides_default_without_merging(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_CONTROL_HOME", str(tmp_path))
    manifest(tmp_path / "config/devices.toml", "TCPIP0::default.invalid::INSTR")
    explicit = manifest(tmp_path / "one selected asset.toml", "TCPIP0::selected.invalid::INSTR")
    assert [x.resource for x in load_startup_config(explicit)] == ["TCPIP0::selected.invalid::INSTR"]


def test_invalid_default_is_reported_and_missing_explicit_does_not_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_CONTROL_HOME", str(tmp_path))
    config = tmp_path / "config/devices.toml"
    config.parent.mkdir()
    config.write_text("devices = []", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_startup_config()
    with pytest.raises(FileNotFoundError):
        load_startup_config(tmp_path / "missing.toml")
