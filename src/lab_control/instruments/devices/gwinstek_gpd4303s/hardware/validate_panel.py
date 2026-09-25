"""Two separate sessions: leave low settings for inspection, then restore on request."""

from __future__ import annotations

import hashlib
import json
import os
import platform
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pyvisa

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceError, jsonable, utc_now
from lab_control.instruments.registry import create_adapter
from lab_control.storage.repository import application_home

from ..adapter import Gpd4303sAdapter
from ..protocol import PacedTransport
from .records import publish_report
from .validate_control import LOW_VOLTAGES, RecordingTransport, TestSession, validate_restoration_target


def panel_restore_target(original: dict, evidence: dict | None) -> dict:
    """Keep raw unknown status intact; a photographed mode is separate evidence."""
    target = deepcopy(original)
    if original["status"]["tracking"] is None:
        if (
            not evidence
            or evidence.get("tracking") != "0"
            or evidence.get("source") != "user_panel_photo"
            or not evidence.get("photo_sha256")
            or original["status"]["output_enabled"] is not False
        ):
            raise DeviceError(
                "Unknown mode requires current independent-mode panel evidence", "invalid_parameter"
            )
        target["status"]["tracking"] = evidence["tracking"]
    return target


def verify_panel(
    config: ConnectionConfig,
    folder: Path,
    *,
    restore_from: Path | None = None,
    reviewed_independent_panel_photo: Path | None = None,
) -> dict[str, Any]:
    """Caller must explicitly request each phase; no delayed automatic restore."""
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "communication.jsonl").touch()
    report: dict[str, Any] = {
        "adapter_id": config.adapter,
        "config": asdict(config),
        "resource": config.resource,
        "expected_serial": config.expected_serial,
        "started_at": utc_now(),
        "scope": "Explicit user-requested panel comparison, output OFF; "
        + (
            "restore after user confirmation"
            if restore_from
            else "leave 1/1.5/2/2.5 V, 0.100 A, LOCAL until user confirmation"
        ),
        "environment": {"python": platform.python_version(), "pyvisa": pyvisa.__version__},
        "source_sha256": {Path(__file__).name: hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
        "stages": {
            key: "not_tested"
            for key in (
                "connection",
                "parameter_read",
                "panel_parameters",
                "panel_confirmation",
                "restoration",
                "physical_output",
            )
        },
        "operations": [],
        "final_state": "Unknown until snapshot",
        "awaiting_user": False,
    }
    app = None
    test = None
    original = None
    recovery_target = None
    changed = False
    stage = "connection"
    try:
        lock = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "TF-LabControl/hardware.lock"
        if (
            lock.resolve() != lock
            or folder.resolve() != folder
            or application_home().resolve() != application_home()
        ):
            raise DeviceError("Native physical data and lock paths required", "path_redirected")
        if config.adapter != "gwinstek.gpd4303s" or config.transport != "visa" or not config.expected_serial:
            raise ValueError("One explicitly identified VISA GPD required")
        target = None
        if reviewed_independent_panel_photo:
            photo = reviewed_independent_panel_photo.read_bytes()
            (folder / "panel-before.png").write_bytes(photo)
            report["panel_evidence"] = {
                "source": "user_panel_photo",
                "photo_sha256": hashlib.sha256(photo).hexdigest(),
                "tracking": "0",
                "observations": "PARA/INDEP and SER/INDEP unlit; OUTPUT unlit",
                "source_note": "Operator-reviewed current user photo; manual p27. "
                "This is not a protocol mapping for undocumented tracking code 00.",
            }
        if restore_from:
            source = json.loads((restore_from / "results.json").read_text(encoding="utf-8"))
            if (
                source.get("expected_serial") != config.expected_serial
                or source.get("resource") != config.resource
                or source.get("stages", {}).get("panel_parameters") != "passed"
                or not source.get("awaiting_user")
            ):
                raise ValueError("Restore record must refer to this exact asset/interface")
            backup = (restore_from / "initial_snapshot.json").read_bytes()
            if hashlib.sha256(backup).hexdigest() != source["initial_snapshot_sha256"]:
                raise ValueError("Original panel-check backup hash mismatch")
            target = panel_restore_target(json.loads(backup), source.get("panel_evidence"))
            validate_restoration_target(target, config)
            report["restore_from"] = str(restore_from)
            report["restore_source_sha256"] = hashlib.sha256(
                (restore_from / "results.json").read_bytes()
            ).hexdigest()
        app = ApplicationRuntime((config,))

        def factory(selected):
            if selected != config:
                raise ValueError("Different asset selected")
            adapter = create_adapter(selected, app.visa)
            assert isinstance(adapter, Gpd4303sAdapter) and isinstance(adapter.transport, PacedTransport)
            adapter.transport.inner = RecordingTransport(
                adapter.transport.inner, folder / "communication.jsonl", True
            )
            return adapter

        app.sessions.factory = factory
        key = app.add(config)
        report["identity"] = jsonable(app.connect(key).result(30))
        report["stages"]["connection"] = "passed"
        stage = "parameter_read"
        test = TestSession(app, key, report, folder)
        original = test.snapshot()
        report["initial_snapshot"] = original
        backup = json.dumps(original, ensure_ascii=False, indent=2).encode("utf-8")
        (folder / "initial_snapshot.json").write_bytes(backup)
        report["initial_snapshot_sha256"] = hashlib.sha256(backup).hexdigest()
        recovery_target = panel_restore_target(original, report.get("panel_evidence"))
        validate_restoration_target(recovery_target, config)
        report["recovery_target"] = recovery_target
        report["stages"]["parameter_read"] = "passed"
        test.save()
        if restore_from:
            stage = "restoration"
            assert target is not None
            test.restore(target)
            report["stages"]["restoration"] = "passed"
        else:
            stage = "panel_parameters"
            changed = True
            if test.read("all_outputs"):
                test.apply("all_outputs", False)
            test.require_off()
            if test.read("status_register")["tracking"] != "0":
                test.apply("tracking", "0")
            for ch, voltage in LOW_VOLTAGES.items():
                test.apply("current", 0.1, ch)
                test.apply("voltage", voltage, ch)
            report["final_snapshot"] = test.snapshot()
            test.require_off()
            report["stages"]["panel_parameters"] = "passed"
            report["awaiting_user"] = True
        test.apply("local")
        report["local_command_sent"] = True
        report["final_output_state"] = report["final_snapshot"]["status"]["output_enabled"]
        report["final_state"] = "Output OFF, LOCAL sent; " + (
            "approved values restored"
            if restore_from
            else "low test values retained awaiting user confirmation"
        )
    except Exception as exc:
        report["error"] = str(exc)
        report["error_code"] = getattr(exc, "code", type(exc).__name__)
        report["stages"][stage] = (
            "blocked"
            if report["error_code"]
            in {
                "timeout",
                "disconnected",
                "connection_failed",
                "resource_busy",
                "process_busy",
                "path_redirected",
            }
            else "failed"
        )
        if changed and test and recovery_target:
            try:
                test.restore(recovery_target)
                test.apply("local")
                report["stages"]["restoration"] = "passed"
                report["final_state"] = (
                    "Panel test failed; original settings restored, output OFF, LOCAL sent"
                )
                report["awaiting_user"] = False
            except Exception as recovery:
                report["restore_error"] = str(recovery)
                report["stages"]["restoration"] = "failed"
                report["final_state"] = "Recovery failed; final state unknown"
    finally:
        if app:
            try:
                app.close()
                report["session_released"] = True
            except Exception as exc:
                report["close_error"] = str(exc)
        report["finished_at"] = utc_now()
        (folder / "results.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        publish_report(folder)
        (folder / "record.md").write_text(
            "# GPD-4303S 前面板核对\n\n"
            + report["scope"]
            + "\n\n"
            + report["final_state"]
            + "\n\n参数见 [results.json](results.json)，通信见 [communication.jsonl](communication.jsonl)。"
            + "原始备份保留且不修改；等待确认期间没有自动恢复任务。物理测量未执行。\n",
            encoding="utf-8",
        )
        from lab_control.storage.hardware_documents import refresh_after_batch

        refresh_after_batch(folder)
    return report
