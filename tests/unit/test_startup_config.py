from pathlib import Path

import pytest

from lab_control.application.config import ConnectionConfig, load_startup_config, save_startup_device
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


def test_save_selected_device_deduplicates_aliases_and_preserves_other_entries(tmp_path):
    path = tmp_path / "devices.toml"
    other = '# User LAN settings\n[[devices]]\nresource="TCPIP0::fixture::INSTR"\nadapter="rs.sma100b"\n'
    path.write_text(
        other
        + '\n[[devices]]\nresource="COM7"\nadapter="gwinstek.gpd4303s"\nexpected_serial="TEST"\n'
        + '\n[[devices]]\nresource="ASRL7::INSTR"\nadapter="gwinstek.gpd4303s"\nexpected_serial="TEST"\n',
        encoding="utf-8",
    )
    target = ConnectionConfig("ASRL7::INSTR", "gwinstek.gpd4303s", expected_serial="TEST")
    save_startup_device(target, path)
    first = path.read_bytes()
    assert other in path.read_text(encoding="utf-8")
    assert len(load_startup_config(path)) == 2
    assert load_startup_config(path)[-1] == target
    save_startup_device(target, path)
    assert path.read_bytes() == first


def test_save_selected_device_rejects_other_serial_without_overwrite(tmp_path):
    path = tmp_path / "devices.toml"
    save_startup_device(ConnectionConfig("COM7", "gwinstek.gpd4303s", expected_serial="FIRST"), path)
    before = path.read_bytes()
    with pytest.raises(ValidationError, match="another asset"):
        save_startup_device(
            ConnectionConfig("ASRL7::INSTR", "gwinstek.gpd4303s", expected_serial="OTHER"), path
        )
    assert path.read_bytes() == before


def test_manifest_replace_failure_keeps_previous_file(tmp_path, monkeypatch):
    path = manifest(tmp_path / "devices.toml", "TCPIP0::other.invalid::INSTR")
    before = path.read_bytes()

    def fail_replace(*_):
        raise OSError("fixture disk failure")

    monkeypatch.setattr("os.replace", fail_replace)
    with pytest.raises(OSError, match="disk failure"):
        save_startup_device(
            ConnectionConfig("ASRL7::INSTR", "gwinstek.gpd4303s", expected_serial="TEST"), path
        )
    assert path.read_bytes() == before and not list(tmp_path.glob("*.tmp"))


def test_manifest_preserves_connection_options_and_limits(tmp_path):
    path = tmp_path / "devices.toml"
    config = ConnectionConfig(
        "ASRL7::INSTR",
        "gwinstek.gpd4303s",
        expected_serial="TEST",
        aliases=("COM7",),
        options=("Fixture",),
        limits={"voltage": 3.2},
        terminator="\r\n",
        sdk_path="C:\\测试\\fixture.dll",
    )
    save_startup_device(config, path)
    assert load_startup_config(path) == (config,)
