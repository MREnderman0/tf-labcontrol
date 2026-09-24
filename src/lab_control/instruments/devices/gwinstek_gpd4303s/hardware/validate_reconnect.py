"""Explicit LOCAL/REMOTE acceptance without electrical parameter/output writes."""

from __future__ import annotations

import hashlib
import json
import os
import platform
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
from .validate_control import RecordingTransport, TestSession, differences


class ConnectionRecording(RecordingTransport):
    def check(self, payload: bytes | str) -> None:
        if payload == "LOCAL":
            return
        # Read-only recording permits REMOTE for identity, but no electrical writes.
        super().check(payload)


def verify_reconnect(config: ConnectionConfig, folder: Path, *, cycles: int = 3) -> dict[str, Any]:
    if not 1 <= cycles <= 10:
        raise ValueError("Use 1 to 10 explicit acceptance cycles")
    folder.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "adapter_id": config.adapter,
        "config": asdict(config),
        "resource": config.resource,
        "expected_serial": config.expected_serial,
        "started_at": utc_now(),
        "environment": {"python": platform.python_version(), "pyvisa": pyvisa.__version__},
        "scope": "LOCAL/REMOTE and close/reopen acceptance; no electrical parameter or output writes",
        "stages": {
            key: "not_tested"
            for key in (
                "connection",
                "same_session_remote",
                "local_reconnect",
                "panel_local",
                "physical_output",
            )
        },
        "operations": [],
        "cycles": [],
        "source_sha256": {
            str(path.name): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (
                Path(__file__),
                Path(__file__).parents[1] / "adapter.py",
                Path(__file__).parents[1] / "protocol.py",
            )
        },
        "final_state": "Not yet observed",
    }
    app = None
    stage = "connection"
    try:
        lock = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "TF-LabControl/hardware.lock"
        if (
            lock.resolve() != lock
            or folder.resolve() != folder
            or application_home().resolve() != application_home()
        ):
            raise DeviceError("Native physical data/lock paths required", "path_redirected")
        if config.adapter != "gwinstek.gpd4303s" or config.transport != "visa" or not config.expected_serial:
            raise ValueError("An explicitly identified VISA GPD is required")
        app = ApplicationRuntime((config,))

        def factory(selected):
            assert selected == config
            adapter = create_adapter(selected, app.visa)
            assert isinstance(adapter, Gpd4303sAdapter) and isinstance(adapter.transport, PacedTransport)
            adapter.transport.inner = ConnectionRecording(
                adapter.transport.inner, folder / "communication.jsonl", False
            )
            return adapter

        app.sessions.factory = factory
        key = app.add(config)
        report["identity"] = jsonable(app.connect(key).result(30))
        report["stages"][stage] = "passed"
        test = TestSession(app, key, report, folder)
        original = test.snapshot()
        test.require_off()
        report["initial_snapshot"] = original
        backup = json.dumps(original, ensure_ascii=False, indent=2).encode("utf-8")
        (folder / "initial_snapshot.json").write_bytes(backup)
        report["initial_snapshot_sha256"] = hashlib.sha256(backup).hexdigest()
        for index in range(cycles):
            item: dict[str, Any] = {"number": index + 1}
            report["cycles"].append(item)
            stage = "same_session_remote"
            test.apply("local")
            if app.sessions.sessions[key].state != "connected":
                raise DeviceError("LOCAL unexpectedly degraded the session", "local_failed")
            item["local_capabilities"] = jsonable(app.sessions.capability_snapshot(key))
            test.apply("remote")
            same = test.snapshot()
            item["same_session"] = same
            if differences(same, original) or same["status"]["output_enabled"]:
                raise DeviceError("Settings/output changed across LOCAL/REMOTE", "readback_mismatch")
            stage = "local_reconnect"
            test.apply("local")
            app.sessions.disconnect(key)
            item["identity"] = jsonable(app.connect(key).result(30))
            reopened = test.snapshot()
            item["reopened"] = reopened
            if differences(reopened, original) or reopened["status"]["output_enabled"]:
                raise DeviceError("Settings/output changed across reopen", "readback_mismatch")
            report["final_snapshot"] = reopened
            item["passed"] = True
            test.save()
        report["stages"]["same_session_remote"] = "passed"
        report["stages"]["local_reconnect"] = "passed"
        test.apply("local")
        report["local_command_sent"] = True
        report["final_output_state"] = False
        report["electrical_writes"] = 0
        report["final_state"] = (
            "Settings unchanged, output verified OFF before final LOCAL; LOCAL sent and no later instrument query; panel check separate"
        )
    except Exception as exc:
        report["stages"][stage] = "blocked"
        report["error"] = str(exc)
        report["final_state"] = (
            "Acceptance interrupted; no electrical writes; last observed snapshot retained, latest communication not verified"
        )
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
            "# LOCAL/REMOTE 重连验收\n\n"
            + report["scope"]
            + "\n\n"
            + report["final_state"]
            + "\n\n[结果](results.json) · [通信](communication.jsonl)。\n",
            encoding="utf-8",
        )
        with (folder.parents[1] / "index.md").open("a", encoding="utf-8") as stream:
            stream.write(
                f"\n- {report['finished_at']}：GPD-4303S LOCAL/REMOTE 验收；[记录]({folder.parent.name}/{folder.name}/record.md)。\n"
            )
    return report
