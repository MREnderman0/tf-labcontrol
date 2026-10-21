import copy
import hashlib
import json
import runpy
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.deployment import environment
from lab_control.deployment.catalog import load_manifest, payload_status, release_issues, required_components
from lab_control.launcher import worker_command

ROOT = Path(__file__).resolve().parents[2]


def test_inventory_and_runtime_import_coverage():
    check = runpy.run_path(str(ROOT / "scripts/check_deployment.py"))["check"]
    result = check()
    assert result["passed"], result
    assert result["pending"]


def test_new_runtime_dependency_cannot_bypass_registration(tmp_path):
    (tmp_path / "deployment").mkdir()
    (tmp_path / "deployment/components.json").write_text(json.dumps(load_manifest()))
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies=["requests>=2"]\n')
    (tmp_path / "uv.lock").write_text('package=[{name="shiboken6"}]')
    source = tmp_path / "src/lab_control"
    source.mkdir(parents=True)
    (source / "new.py").write_text("import requests\n")
    result = runpy.run_path(str(ROOT / "scripts/check_deployment.py"))["check"](tmp_path)
    assert not result["passed"]
    assert any("requests" in error for error in result["errors"])


def test_build_does_not_collect_native_libraries_from_other_applications(monkeypatch):
    monkeypatch.setenv("PATH", "C:\\UnrelatedApp\\bin")
    monkeypatch.setenv("PYTHONPATH", "C:\\OtherSource")
    monkeypatch.setenv("QT_PLUGIN_PATH", "C:\\OtherQt")
    environment = runpy.run_path(str(ROOT / "scripts/build_desktop.py"))["build_environment"]()
    assert "UnrelatedApp" not in environment["PATH"]
    assert "PYTHONPATH" not in environment
    assert "QT_PLUGIN_PATH" not in environment


def test_unknown_payload_install_method_and_redistribution_block_release(tmp_path):
    component = load_manifest()["components"][0]
    problems = release_issues(component, (tmp_path,))
    assert "offline payload missing" in problems
    assert "redistribution not verified" in problems
    assert "installation arguments/exit codes pending" in problems
    incomplete = copy.deepcopy(component)
    incomplete["architecture"] = "x86"
    from lab_control.deployment.catalog import validate_manifest

    assert any("architecture" in e for e in validate_manifest({"components": [incomplete]}))


def test_payload_hash_missing_corrupt_valid_and_cancel(tmp_path):
    component = {"filename": "installer.exe", "sha256": hashlib.sha256(b"fixture").hexdigest()}
    assert payload_status(component, (tmp_path,))["state"] == "missing"
    path = tmp_path / component["filename"]
    path.write_bytes(b"bad")
    assert payload_status(component, (tmp_path,))["state"] == "invalid"
    path.write_bytes(b"fixture")
    assert payload_status(component, (tmp_path,))["state"] == "verified"
    cancel = threading.Event()
    cancel.set()
    assert payload_status(component, (tmp_path,), cancel)["state"] == "cancelled"


def test_model_refs_and_unused_optional_components():
    refs = required_components((ConnectionConfig("ASRL3::INSTR", "gwinstek.gpd4303s"),))
    assert {"rs-visa", "ftdi-vcp"} <= refs
    assert "pico-sdk" not in refs
    refs = required_components((ConnectionConfig("tcp://example.invalid:5025", "rs.sma100b", "tcp"),))
    assert "rs-visa" not in refs


def test_inspection_never_opens_visa_or_devices_and_preserves_partial_result(tmp_path, monkeypatch):
    import pyvisa

    monkeypatch.setattr(pyvisa, "ResourceManager", lambda *a: pytest.fail("VISA manager opened"))
    monkeypatch.setattr("lab_control.deployment.windows.inspect_windows", lambda cancel: {})
    cancel = threading.Event()
    seen = []

    def progress(report):
        seen.append(report)
        if len(report["items"]) == 3:
            cancel.set()

    report = environment.inspect_environment(tmp_path, cancel=cancel, progress=progress, caches=(tmp_path,))
    assert report["state"] == "cancelled"
    assert len(report["items"]) == 3
    assert not report["repair_plan"]["executed"]
    assert not report["repair_plan"]["inspection_complete"]
    assert not list(tmp_path.iterdir())


def test_inspection_reports_optional_driver_unknown_and_storage_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("lab_control.deployment.windows.inspect_windows", lambda cancel: {})
    monkeypatch.setattr(environment, "_storage", lambda root: (_ for _ in ()).throw(OSError("read-only")))
    monkeypatch.setattr(environment, "expected_versions", lambda: {"numpy": "0.0"})
    report = environment.inspect_environment(tmp_path, caches=(tmp_path,))
    by_id = {item["id"]: item for item in report["items"]}
    assert by_id["pico-sdk"]["state"] == "not_required"
    assert by_id["storage"]["state"] == "error"
    assert by_id["msvc-runtime"]["state"] == "unknown"
    assert by_id["python:numpy"]["state"] == "incompatible"
    assert "pytest" not in by_id
    path = tmp_path / "中文 report.json"
    environment.export_report(report, path)
    assert json.loads(path.read_text(encoding="utf-8")) == report


def test_inspection_handles_model_specific_sdk_without_generic_checker(tmp_path, monkeypatch):
    from lab_control.application.config import ConnectionConfig

    monkeypatch.setattr("lab_control.deployment.windows.inspect_windows", lambda cancel: {})
    report = environment.inspect_environment(
        tmp_path,
        (ConnectionConfig("192.0.2.10", "kplusk.fxe", "kplusk"),),
        caches=(tmp_path,),
    )
    by_id = {item["id"]: item for item in report["items"]}
    assert report["state"] == "complete"
    assert by_id["kplusk-library-win"]["state"] == "unknown"
    assert "未登记通用 DLL 检测器" in by_id["kplusk-library-win"]["reason"]


@pytest.mark.parametrize(
    ("version", "expected"),
    [("v14.51.36247.0", "ok"), ("v14.34.31823.03", "incompatible")],
)
def test_vc_runtime_detection_never_accepts_an_older_version(tmp_path, monkeypatch, version, expected):
    monkeypatch.setattr(
        "lab_control.deployment.windows.inspect_windows",
        lambda cancel: {"vc_runtime": {"Installed": 1, "Version": version}},
    )
    report = environment.inspect_environment(tmp_path, caches=(tmp_path,))
    item = next(item for item in report["items"] if item["id"] == "msvc-runtime")
    assert item["state"] == expected


def test_frozen_worker_command_and_dispatch_before_qt(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    command = worker_command(tmp_path)
    assert command == [sys.executable, "--csv-worker", str(tmp_path.resolve())]


def test_source_worker_real_process_handles_unicode_path_without_hardware(tmp_path):
    target = tmp_path / "中文 folder"
    target.mkdir()
    # Corrupt committed input must fail visibly, preserving its bytes.
    import numpy as np

    from lab_control.domain.instruments import DeviceIdentity, Measurement, MeasurementBatch
    from lab_control.storage.recording import SegmentWriter

    writer = SegmentWriter(target)
    path = Path(
        writer.append(
            1,
            MeasurementBatch(DeviceIdentity("Fixture", "CSV"), (Measurement("test", "V", np.arange(4)),), {}),
        )
    )
    writer.close()
    result = subprocess.run(worker_command(target), cwd=tmp_path, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads((target / "csv-status.json").read_text())["converted"] == 1
    assert path.with_suffix(".csv").exists()
    assert not list(tmp_path.rglob("hardware.lock"))


def test_source_worker_failure_is_reported_and_does_not_delete_input(tmp_path):
    path = tmp_path / "segment-000001.sqlite3"
    path.write_bytes(b"broken sqlite fixture")
    failed = subprocess.run(worker_command(tmp_path), capture_output=True, timeout=15)
    assert failed.returncode == 1
    assert json.loads((tmp_path / "csv-status.json").read_text())["error"]
    result = subprocess.run(worker_command(tmp_path / "missing"), capture_output=True, timeout=15)
    assert result.returncode == 2
    assert path.read_bytes() == b"broken sqlite fixture"
