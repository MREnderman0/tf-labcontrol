from __future__ import annotations

import copy
import hashlib
import json
import runpy
from pathlib import Path

import pytest

from lab_control.instruments.devices.tek_afg31252.hardware.records import summarize
from lab_control.storage.hardware_records import normalize, publish, validate

ROOT = Path(__file__).resolve().parents[2]
TOOL = runpy.run_path(str(ROOT / "scripts/hardware_records.py"))


def report(serial="TEST-001", interface="USB0::TEST::INSTR", stages=None):
    return {
        "adapter_id": "tek.afg31252",
        "resource": interface,
        "expected_serial": serial,
        "identity": {"manufacturer": "TEKTRONIX", "model": "AFG31252", "serial": serial, "firmware": "test"},
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:00:01Z",
        "scope": "Output OFF; test fixture",
        "environment": {"python": "test"},
        "project_head": "fixture",
        "stages": stages or {"basic": "passed", "restoration": "failed", "physical_output": "not_tested"},
        "final_outputs": {"1": False, "2": False},
    }


def batch(root, data, name="2026-01-01-000000-control"):
    folder = root / ("AFG31252-" + data.get("expected_serial", "unknown")) / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "results.json").write_text(json.dumps(data), encoding="utf-8")
    (folder / "record.md").write_text("Original evidence", encoding="utf-8")
    (folder.parent / "connection-status.md").write_text("Manual default USB; LAN blocked", encoding="utf-8")
    return folder


def test_versioned_summary_never_guesses_recovery_or_output(tmp_path):
    data = report()
    data.pop("final_outputs")
    data["stages"]["restoration"] = "passed_in_separate_recovery"
    summary = summarize(data)
    assert summary["final_state"] == {}
    assert next(i for i in summary["items"] if i["key"] == "restoration")["status"] == "not_tested"
    assert summary["completeness"] == "needs_review"
    assert not validate(summary)
    folder = batch(tmp_path, data)
    publish(folder, summary)
    saved = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    assert saved["evidence"]["sha256"] == hashlib.sha256((folder / "results.json").read_bytes()).hexdigest()


def test_twenty_assets_multi_interface_readonly_recovery_and_idempotence(tmp_path, monkeypatch):
    import pyvisa

    monkeypatch.setattr(pyvisa, "ResourceManager", lambda *a, **kw: pytest.fail("Must not load VISA"))
    for index in range(20):
        batch(tmp_path, report(f"TEST-{index:03}"))
    earlier = report()
    earlier["resource"] = "TCPIP0::192.0.2.10::INSTR"
    batch(tmp_path, earlier, "2026-01-01-010000-lan")
    recovery = report(
        stages={"basic": "not_tested", "restoration": "passed", "physical_output": "not_tested"}
    )
    recovery["started_at"] = "2026-01-02T00:00:00Z"
    batch(tmp_path, recovery, "2026-01-02-000000-recovery")
    readonly = report(stages={"basic": "not_tested", "parameter_read": "passed"})
    readonly["started_at"] = "2026-01-03T00:00:00Z"
    batch(tmp_path, readonly, "2026-01-03-000000-readonly")
    original = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    summaries = TOOL["import_records"](tmp_path, [tmp_path])
    TOOL["build_index"](tmp_path, summaries)
    assert len(summaries) == 23
    output = tmp_path / "overview/README.md"
    content = output.read_text(encoding="utf-8")
    assert content.count("## TEKTRONIX / AFG31252 /") == 20
    assert "| USB | basic | test | passed |" in content
    assert "| LAN | basic | test | passed |" in content
    assert "restoration=failed" in content and "restoration=passed" in content
    before = output.read_bytes()
    TOOL["build_index"](tmp_path, TOOL["import_records"](tmp_path, [tmp_path]))
    assert output.read_bytes() == before
    assert all(p.read_bytes() == data for p, data in original.items())
    assert TOOL["check_records"](tmp_path) == []


def test_ip_changes_and_identity_conflicts_do_not_merge_by_address(tmp_path):
    a = report("TEST-A", "TCPIP0::192.0.2.10::INSTR")
    b = report("TEST-B", "TCPIP0::192.0.2.10::INSTR")
    batch(tmp_path, a)
    batch(tmp_path, b)
    a["resource"] = "TCPIP0::192.0.2.11::INSTR"
    batch(tmp_path, a, "2026-01-02-moved")
    conflict = copy.deepcopy(a)
    conflict["expected_serial"] = "WRONG"
    batch(tmp_path, conflict, "2026-01-03-conflict")
    summaries = TOOL["import_records"](tmp_path, [tmp_path])
    TOOL["build_index"](tmp_path, summaries)
    text = (tmp_path / "overview/README.md").read_text(encoding="utf-8")
    assert text.count("## TEKTRONIX / AFG31252 / TEST-A") == 1
    assert text.count("## TEKTRONIX / AFG31252 / TEST-B") == 1
    assert "身份未核验" in text


def test_wrong_model_and_unknown_serial_are_not_reliable():
    data = report()
    data["identity"]["model"] = "DIFFERENT MODEL"
    assert not summarize(data)["identity_verified"]
    data = report("UNKNOWN")
    assert not summarize(data)["identity_verified"]


def test_unknown_damaged_empty_and_changed_evidence(tmp_path):
    folder = batch(tmp_path, report())
    (folder / "results.json").write_text("{invalid", encoding="utf-8")
    empty = folder.parent / "2026-01-02-empty"
    empty.mkdir()
    unknown = batch(tmp_path, dict(report(), adapter_id="removed.device"), "2026-01-03-unknown")
    summaries = TOOL["import_records"](tmp_path, [tmp_path])
    assert len(summaries) == 3 and all(s["issues"] for s in summaries)
    assert not next(s for s in summaries if s["batch"] == empty.name)["identity_verified"]
    (unknown / "results.json").write_text("{}", encoding="utf-8")
    assert any("Source changed" in message for message in TOOL["check_records"](tmp_path))


def test_additional_source_is_readonly(tmp_path):
    root = tmp_path / "formal"
    source = tmp_path / "historical"
    root.mkdir()
    folder = batch(source, report())
    before = {p: p.read_bytes() for p in source.rglob("*") if p.is_file()}
    summaries = TOOL["import_records"](root, [root, source])
    assert summaries[0]["source_folder"] == str(folder.resolve())
    assert all(p.read_bytes() == data for p, data in before.items())
    assert not (source / "overview").exists()


def test_summary_failure_does_not_interrupt_recovery(tmp_path, monkeypatch, caplog):
    from lab_control.storage import hardware_records

    folder = batch(tmp_path, report())
    original = (folder / "results.json").read_bytes()

    def fail(*args):
        raise OSError("summary file locked")

    with monkeypatch.context() as patch:
        patch.setattr(hardware_records, "publish", fail)
        hardware_records.publish_report(folder, summarize)
    assert "摘要生成失败" in caplog.text
    assert (folder / "summary-error.json").exists()
    assert (folder / "results.json").read_bytes() == original
    hardware_records.publish_report(folder, summarize)
    assert (folder / "summary.json").exists()
    assert not (folder / "summary-error.json").exists()


def test_manifest_path_escape_rejected(tmp_path):
    (tmp_path / "overview").mkdir()
    (tmp_path / "overview/manifest.json").write_text(
        json.dumps({"schema_version": 1, "summaries": ["../../unrelated.json"]})
    )
    assert TOOL["check_records"](tmp_path) == ["Invalid manifest summary path"]


@pytest.mark.parametrize("stages", [[], {"x": {}}, {"x": {"state": []}}])
def test_malformed_stages_remain_reviewable(stages):
    summary = normalize({"stages": stages}, adapter_id="test", model="test", stage_names={})
    assert summary["completeness"] == "needs_review"
    assert not validate(summary)
