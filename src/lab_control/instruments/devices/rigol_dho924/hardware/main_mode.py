"""Authorized MAIN-policy verification; retain the requested final MAIN state."""

import hashlib
import os
from pathlib import Path
from typing import Any

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest, DeviceError, jsonable, utc_now
from lab_control.instruments.registry import create_adapter
from lab_control.storage.hardware_documents import refresh_after_batch
from lab_control.storage.repository import application_home

from ..adapter import DHO924Adapter
from ..profile import MAIN_TIMEBASE_MAX
from .records import publish_report
from .verify import AuditTransport, differences, save, snapshot, verify_paths


def verify_main(config: ConnectionConfig, folder: Path) -> dict:
    """Back up read-only, then test normal connection setup and three passive frames.

    Caller must authorize leaving MAIN and the bounded timebase in place. Never
    force ROLL merely to exercise a test or restore the unwanted rolling mode.
    """
    import platform

    import pyvisa

    if config.adapter != "rigol.dho924" or not config.expected_serial:
        raise ValueError("Select one exact DHO924 asset with expected serial")
    report: dict[str, Any] = {
        "adapter_id": config.adapter,
        "resource": config.resource,
        "expected_serial": config.expected_serial,
        "started_at": utc_now(),
        "scope": "Authorized MAIN policy: read-only backup, normal connection setup, three passive screen frames; inputs not inspected; leave requested MAIN state",
        "environment": {"python": platform.python_version(), "pyvisa": pyvisa.__version__},
        "project_head": os.environ.get("LAB_TEST_PROJECT_HEAD", "unavailable"),
        "source_sha256": {
            str(path.relative_to(Path(__file__).parents[1])): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in Path(__file__).parents[1].rglob("*.py")
            if "tests" not in path.parts
        },
        "stages": {
            name: "not_tested"
            for name in (
                "identity",
                "connection",
                "parameter_read",
                "parameter_write",
                "waveform",
                "monitoring",
                "recording",
                "restoration",
                "reconnection",
                "physical_output",
            )
        },
    }
    app = None
    stage = "connection"
    allow_writes = False
    try:
        report["paths"] = verify_paths(application_home())
        app = ApplicationRuntime((config,))
        app.monitoring.enable_settings_sync(False)

        def factory(selected):
            obj = create_adapter(selected, app.visa)
            if not isinstance(obj, DHO924Adapter):
                obj.close()
                raise DeviceError("Factory mismatch", "identity_conflict")
            obj.transport = AuditTransport(obj.transport, folder)
            obj.transport.allow_writes = allow_writes
            return obj

        app.sessions.factory = factory
        key = app.add(config)
        report["identity"] = jsonable(app.connect(key, configure_acquisition=False).result(60))
        report["stages"].update(identity="passed", connection="passed")
        session = app.sessions.sessions[key]
        assert isinstance(session.adapter, DHO924Adapter)
        initial = session.worker.submit(snapshot, session.adapter).result(60)
        report["initial_snapshot"] = initial
        save(folder / "initial.json", initial)
        report["stages"]["parameter_read"] = "passed"
        app.sessions.disconnect(key)
        allow_writes = True
        stage = "parameter_write"
        app.connect(key).result(60)
        session = app.sessions.sessions[key]
        assert isinstance(session.adapter, DHO924Adapter)
        report["preparation"] = session.acquisition_preparation
        report["stages"]["reconnection"] = "passed"
        final = session.worker.submit(snapshot, session.adapter).result(60)
        report["setup_snapshot"] = final
        save(folder / "after-setup.json", final)
        delta = differences(initial, final)
        report["intentional_changes"] = delta
        allowed = {
            "values.1.timebase",
            "values.1.timebase_mode",
            "values.1.time_position",
            "values.1.sample_rate",
            "values.1.record_length",
            "transfer.transfer_format",
        }
        unexpected = {key: value for key, value in delta.items() if key not in allowed}
        report["unexpected_changes"] = unexpected
        if unexpected or final["values"]["1.timebase_mode"] != "MAIN":
            raise DeviceError("MAIN setup verification differs", "readback_mismatch")
        if initial["values"]["1.timebase_mode"] == "ROLL":
            if final["values"]["1.timebase"] > MAIN_TIMEBASE_MAX:
                raise DeviceError("MAIN scale exceeds software limit", "readback_mismatch")
            report["stages"][stage] = "passed"
        else:
            report["mode_transition_note"] = "Already MAIN; no forced ROLL or transition claim"
        stage = "waveform"
        enabled = tuple(ch for ch in range(1, 5) if final["values"][f"{ch}.channel_enabled"])
        if not enabled:
            report["stages"][stage] = "blocked"
            report["waveform_note"] = "No enabled channel; channel settings left unchanged"
        else:
            request = AcquisitionRequest(
                "waveform", (enabled[0],), consistency="live", context_id="main-policy-check"
            )
            report["frames"] = []
            for _ in range(3):
                batch = app.sessions.submit(key, "acquire", request).result(60)
                report["frames"].append(
                    {
                        "validation": batch.validation,
                        "points": len(batch.measurements[0].values),
                        "configuration": batch.configuration,
                        "performance": batch.performance,
                    }
                )
            report["stages"][stage] = "passed"
        stage = "transfer_restoration"
        session.worker.submit(session.adapter.restore_transfer, initial["transfer"]).result(60)
        report["stages"][stage] = "passed"
        final = session.worker.submit(snapshot, session.adapter).result(60)
        report["final_snapshot"] = final
        report["final_differences"] = differences(initial, final)
        save(folder / "final.json", final)
        report["final_state"] = (
            "Requested MAIN state retained; original ROLL intentionally not restored. No channel, trigger, RUN or STOP writes. Software session released."
        )
    except Exception as exc:
        report.update(error=str(exc), error_code=getattr(exc, "code", type(exc).__name__))
        report["stages"][stage] = (
            "blocked"
            if report["error_code"]
            in {"resource_busy", "process_busy", "path_redirected", "connection_failed"}
            else "failed"
        )
        report["final_state"] = (
            "Stopped after failure without compensation/replay; see last confirmed snapshot and communication log"
        )
    finally:
        if app:
            try:
                app.close()
                report["session_released"] = True
            except Exception as exc:
                report["close_error"] = str(exc)
        report["finished_at"] = utc_now()
        save(folder / "results.json", report)
        (folder / "record.md").write_text(
            "# DHO924 MAIN policy\n\n"
            + report["scope"]
            + "\n\n"
            + report.get("error", "")
            + "\n\n"
            + report.get("final_state", ""),
            encoding="utf-8",
        )
        publish_report(folder)
        refresh_after_batch(folder)
    return report
