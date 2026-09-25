from __future__ import annotations

import json
import runpy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.instruments.common.hardware_acceptance import enumerate_configured, visa_interface
from lab_control.storage.hardware_documents import MARKER, update_documents
from lab_control.storage.hardware_records import normalize, publish


@pytest.mark.parametrize(
    "resource,expected",
    [
        ("USB0::0x0001::0x0002::TEST::INSTR", "USB"),
        ("TCPIP0::192.0.2.1::inst0::INSTR", "LAN"),
        ("TCPIP::example.invalid::hislip0::INSTR", "LAN"),
    ],
)
def test_explicit_serial_bound_resources(resource, expected):
    assert visa_interface(ConnectionConfig(resource, expected_serial="TEST-001")) == expected


@pytest.mark.parametrize(
    "resource,serial",
    [
        ("TCPIP0::*::INSTR", "TEST"),
        ("USB?*::INSTR", "TEST"),
        ("TCPIP0::192.0.2.1::5025::SOCKET", "TEST"),
        ("ASRL3::INSTR", "TEST"),
        ("TCPIP0::192.0.2.1::INSTR", ""),
    ],
)
def test_ambiguous_or_unqualified_resource_rejected(resource, serial):
    with pytest.raises(ValueError):
        visa_interface(ConnectionConfig(resource, expected_serial=serial))


def test_lan_enumeration_does_not_invent_presence_or_query_identity():
    runtime = SimpleNamespace(discover_lan=lambda timeout: (), manager=None)
    config = ConnectionConfig("TCPIP0::192.0.2.1::INSTR", expected_serial="TEST")
    assert enumerate_configured(runtime, config) == ()


def make_batch(root, model, serial, name, interface="LAN", stages=None, **extra):
    folder = root / f"{model}-{serial}" / name
    folder.mkdir(parents=True)
    report = dict(
        identity=dict(manufacturer="Fixture", model=model, serial=serial, firmware="1"),
        resource="TCPIP0::192.0.2.1::INSTR" if interface == "LAN" else "USB0::TEST::INSTR",
        expected_serial=serial,
        started_at=name,
        finished_at=name,
        scope="outputs OFF",
        environment={"fixture": True},
        project_head="fixture",
        stages=stages or {"identity": "passed"},
        **extra,
    )
    (folder / "results.json").write_text(json.dumps(report), encoding="utf-8")
    (folder / "record.md").write_text("Original batch", encoding="utf-8")
    summary = normalize(report, adapter_id="fixture", model=model, stage_names={})
    publish(folder, summary)
    return folder


def test_all_categories_structure_history_and_repeatability_without_visa(tmp_path, monkeypatch):
    import pyvisa

    monkeypatch.setattr(pyvisa, "ResourceManager", lambda *a, **k: pytest.fail("Offline only"))
    for i, model in enumerate(("Source", "Supply", "Spectrum", "Scope", "Meter")):
        make_batch(
            tmp_path,
            model,
            f"TEST-{i}",
            "2026-01-01-control",
            stages={"basic": "passed", "restoration": "failed"},
        )
    recovery = make_batch(
        tmp_path,
        "Source",
        "TEST-0",
        "2026-01-02-recovery",
        stages={"restoration": "passed"},
        restore_from="2026-01-01-control",
    )
    make_batch(
        tmp_path,
        "Source",
        "TEST-0",
        "2026-01-03-readonly",
        stages={"basic": "not_tested", "parameter_read": "passed"},
    )
    make_batch(tmp_path, "Source", "TEST-0", "2026-01-04-usb", interface="USB", stages={"basic": "blocked"})
    manual = recovery.parent / "connection-status.md"
    manual.write_text("Manual note with [evidence](2026-01-01-control/record.md)", encoding="utf-8")
    (tmp_path / "index.md").write_text("Legacy human notes", encoding="utf-8")
    raw = {p: p.read_bytes() for p in tmp_path.glob("*/*/*") if p.is_file()}
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: update_documents(tmp_path), range(4)))
    text = manual.read_text(encoding="utf-8")
    assert text.startswith(MARKER)
    for title in ("当前结论", "接口验证矩阵", "功能覆盖表", "已知问题", "批次索引"):
        assert "## " + title in text
    assert "| LAN | basic | 1 | passed |" in text
    assert "| USB | basic | 1 | blocked |" in text
    assert "restoration=failed" in text and "restoration=passed" in text
    assert "connection-status-before-structured-" in text
    assert all(p.read_bytes() == data for p, data in raw.items())
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.md")}
    update_documents(tmp_path)
    assert all(p.read_bytes() == data for p, data in before.items())
    assert len(list(tmp_path.glob("*/connection-status.md"))) == 5
    assert (
        sum(
            line.startswith("| 2026-01-01-control |")
            for line in (tmp_path / "index.md").read_text(encoding="utf-8").splitlines()
        )
        == 5
    )


def test_legacy_sources_readonly_and_unverified_identity_not_promoted(tmp_path):
    root, legacy = tmp_path / "formal", tmp_path / "legacy"
    root.mkdir()
    folder = make_batch(legacy, "AFG31252", "TEST-OLD", "2026-01-01-control")
    data = json.loads((folder / "results.json").read_text(encoding="utf-8"))
    data["adapter_id"] = "tek.afg31252"
    (folder / "results.json").write_text(json.dumps(data), encoding="utf-8")
    originals = {p: p.read_bytes() for p in legacy.rglob("*") if p.is_file()}
    tool = runpy.run_path(str(Path(__file__).parents[2] / "scripts/hardware_records.py"))
    tool["import_records"](root, [root, legacy])
    update_documents(root)
    assert all(p.read_bytes() == v for p, v in originals.items())
    assert (root / "AFG31252-TEST-OLD/connection-status.md").exists()
    make_batch(root, "Unknown", "UNKNOWN", "2026-01-02-readonly")
    update_documents(root)
    assert not (root / "Unknown-UNKNOWN/connection-status.md").exists()


def test_protocols_do_not_share_qualification_and_asset_alias_preserves_notes(tmp_path):
    vxi = make_batch(
        tmp_path,
        "Source",
        "TEST-001",
        "2026-01-01-vxi",
        stages={"connection": "failed", "basic": "passed"},
    )
    socket = make_batch(
        tmp_path,
        "Source",
        "TEST-001",
        "2026-01-02-socket",
        stages={"connection": "passed", "basic": "not_tested"},
    )
    raw = json.loads((socket / "results.json").read_text(encoding="utf-8"))
    raw["resource"] = "TCPIP0::192.0.2.1::5025::SOCKET"
    (socket / "results.json").write_text(json.dumps(raw), encoding="utf-8")
    publish(socket, normalize(raw, adapter_id="fixture", model="Source", stage_names={}))
    alias = tmp_path / "Source-old-folder"
    alias.mkdir()
    vxi.rename(alias / vxi.name)
    note = alias / "connection-status.md"
    note.write_text("Original human note", encoding="utf-8")
    update_documents(tmp_path)
    text = (socket.parent / "connection-status.md").read_text(encoding="utf-8")
    assert "| connection | 1 | passed | VISA SOCKET" in text
    assert "| connection | 1 | failed | VXI-11" in text
    assert "| basic | 1 | passed | VXI-11" in text
    assert "| basic | 1 | not_tested | VISA SOCKET" in text
    assert "查看统一结构化测试状态" in note.read_text(encoding="utf-8")
    assert next(alias.glob("connection-status-before-structured-*.md")).read_text() == "Original human note"
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.md")}
    update_documents(tmp_path)
    assert all(p.read_bytes() == value for p, value in before.items())


def test_documentation_failure_cannot_escape_cleanup_even_if_diagnostics_unwritable(tmp_path, monkeypatch):
    from lab_control.storage import hardware_documents, hardware_records

    def fail(*args, **kwargs):
        raise PermissionError("Read only fixture")

    monkeypatch.setattr(hardware_documents, "update_documents", fail)
    monkeypatch.setattr(hardware_records, "atomic_json", fail)
    hardware_documents.refresh_after_batch(tmp_path / "asset" / "batch")


def test_empty_batch_stays_indexed_without_link_to_missing_result(tmp_path):
    folder = tmp_path / "Unknown" / "2026-01-01-empty"
    folder.mkdir(parents=True)
    tool = runpy.run_path(str(Path(__file__).parents[2] / "scripts/hardware_records.py"))
    tool["import_records"](tmp_path, [tmp_path])
    update_documents(tmp_path)
    index = (tmp_path / "index.md").read_text(encoding="utf-8")
    assert "2026-01-01-empty" in index
    assert "results.json" not in index
    assert "待核对" in index
