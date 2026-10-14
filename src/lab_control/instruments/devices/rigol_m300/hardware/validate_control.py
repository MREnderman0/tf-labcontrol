"""Run the explicit M300 LAN acceptance plan and preserve runtime evidence."""

from __future__ import annotations

import argparse
import json
import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceCommand, DeviceError, jsonable, utc_now
from lab_control.storage.repository import application_home

from .records import publish_report

STATUS_FIELDS = (
    "scan_list",
    "scan_size",
    "dmm_installed",
    "dmm_state",
    "trigger_source",
    "trigger_interval",
    "monitor_state",
    "slot_100",
    "slot_200",
    "slot_300",
    "slot_400",
    "slot_500",
    "errors",
)


class RecordingTransport:
    """Log the exact SCPI request and response while preserving the session owner."""

    def __init__(self, inner: Any, path: Path):
        self.inner, self.resource, self.path = inner, inner.resource, path

    def _write(self, value: bytes | str) -> None:
        raw = value if isinstance(value, bytes) else value.encode("ascii", "backslashreplace")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"time": utc_now(), "direction": "tx", "ascii": raw.decode("ascii", "backslashreplace"), "hex": raw.hex()}, ensure_ascii=False) + "\n")

    def _read(self, raw: bytes) -> bytes:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"time": utc_now(), "direction": "rx", "ascii": raw.decode("ascii", "backslashreplace"), "hex": raw.hex()}, ensure_ascii=False) + "\n")
        return raw

    def write(self, payload: bytes | str) -> None:
        self._write(payload)
        self.inner.write(payload)

    def read(self) -> bytes:
        return self._read(self.inner.read())

    def query(self, payload: bytes | str) -> bytes:
        self._write(payload)
        return self._read(self.inner.query(payload))

    def open(self) -> None:
        self.inner.open()

    def close(self) -> None:
        self.inner.close()


def save_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(jsonable(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resource", required=True)
    parser.add_argument("--expected-serial", required=True)
    parser.add_argument("--home", type=Path, default=application_home())
    args = parser.parse_args(argv)
    config = ConnectionConfig(args.resource, "rigol.m300", "visa", expected_serial=args.expected_serial, timeout=5)
    started = utc_now()
    asset_root = args.home / "hardware-tests" / f"RIGOL-M300-{args.expected_serial}"
    batch = asset_root / (datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-lan-control")
    batch.mkdir(parents=True, exist_ok=False)
    communication = batch / "communication.jsonl"
    report: dict[str, Any] = {
        "schema_version": 1,
        "adapter_id": "rigol.m300",
        "model": "M300",
        "expected_serial": args.expected_serial,
        "resource": args.resource,
        "interface": "LAN",
        "started_at": started,
        "scope": "LAN VISA identity, module/status readback, trigger interval write/readback/recovery, reconnect; no scan-list or physical-output changes",
        "project_head": "working-tree",
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "stages": {
            "enumeration": {"state": "passed", "resource": args.resource},
            "identity": {"state": "not_tested"},
            "compatibility_connection": {"state": "not_tested"},
            "parameter_control": {"state": "not_tested"},
            "recovery": {"state": "not_tested"},
            "reconnect": {"state": "not_tested"},
            "acquisition": {"state": "not_tested"},
            "physical_output": {"state": "not_tested", "reason": "M300 has no external output measurement in this plan"},
        },
    }
    initial: dict[str, Any] = {}
    final: dict[str, Any] = {}
    app: ApplicationRuntime | None = None
    device_id = ""
    try:
        app = ApplicationRuntime((config,), root=args.home, hardware=True)
        device_id = app.add(config)
        identity = app.connect(device_id, configure_acquisition=False).result()
        report["identity"] = identity
        report["stages"]["identity"] = {"state": "passed", "raw": identity.raw}
        report["stages"]["compatibility_connection"] = {"state": "passed", "adapter": "rigol.m300"}
        session = app.sessions.sessions[device_id]
        if session.adapter is not None:
            adapter_obj: Any = session.adapter
            adapter_obj.transport = RecordingTransport(adapter_obj.transport, communication)
        status = app.sessions.submit(device_id, "status").result()
        initial = jsonable(status.values)
        report["initial_snapshot"] = initial
        save_json(batch / "initial_snapshot.json", initial)
        if int(initial.get("scan_size") or 0) <= 0:
            report["stages"]["acquisition"] = {"state": "blocked", "reason": "scan list is empty; no channel configuration or READ? was authorized"}
        original_interval = float(initial["trigger_interval"])
        target = original_interval + 1.0 if original_interval <= 359998.999 else original_interval - 1.0
        changed = app.sessions.submit(device_id, "apply", DeviceCommand("trigger_interval", {"value": target})).result()
        after_change = jsonable(app.sessions.submit(device_id, "status").result().values)
        if changed.outcome != "success" or abs(float(after_change["trigger_interval"]) - target) > 1e-6:
            raise DeviceError("M300 trigger interval write/readback failed", "readback_mismatch")
        report["stages"]["parameter_control"] = {"state": "passed", "requested": target, "readback": after_change["trigger_interval"]}
        restored = app.sessions.submit(device_id, "apply", DeviceCommand("trigger_interval", {"value": original_interval})).result()
        after_restore = jsonable(app.sessions.submit(device_id, "status").result().values)
        if restored.outcome != "success" or abs(float(after_restore["trigger_interval"]) - original_interval) > 1e-6:
            report["stages"]["recovery"] = {"state": "failed", "reason": "trigger interval did not return to original value", "actual": after_restore.get("trigger_interval")}
            raise DeviceError("M300 recovery readback failed", "restoration_failed")
        report["stages"]["recovery"] = {"state": "passed", "expected": original_interval, "actual": after_restore["trigger_interval"]}
        app.sessions.disconnect(device_id)
        identity_again = app.connect(device_id, configure_acquisition=False).result()
        report["stages"]["reconnect"] = {"state": "passed", "raw": identity_again.raw}
        session = app.sessions.sessions[device_id]
        if session.adapter is not None:
            adapter_obj = session.adapter
            adapter_obj.transport = RecordingTransport(adapter_obj.transport, communication)
        final = jsonable(app.sessions.submit(device_id, "status").result().values)
        report["final_state"] = final
        report["final_outputs"] = {"monitor_state": final.get("monitor_state"), "dmm_state": final.get("dmm_state")}
    except Exception as exc:
        report["error"] = str(exc)
        report["control_error"] = getattr(exc, "code", "device_error")
        for key, value in report["stages"].items():
            if value.get("state") == "not_tested":
                value["reason"] = "stopped after prior failure"
        if report["stages"]["recovery"]["state"] == "not_tested" and initial:
            report["restoration_warning"] = "Recovery could not be confirmed; final instrument state is unknown"
    finally:
        if app is not None:
            try:
                if device_id and device_id in app.sessions.sessions:
                    app.sessions.disconnect(device_id)
            except Exception as exc:
                report["disconnect_error"] = str(exc)
            try:
                app.close()
            except Exception as exc:
                report["close_error"] = str(exc)
    report["finished_at"] = utc_now()
    report["final_observed_snapshot"] = final
    save_json(batch / "results.json", report)
    publish_report(batch)
    (batch / "record.md").write_text(
        "# RIGOL M300 LAN 实机测试\n\n"
        f"- 资源：`{args.resource}`\n- 序列号：`{args.expected_serial}`\n- 起止：{started} → {report['finished_at']}\n\n"
        + "\n".join(f"- {key}: **{value.get('state')}**；{value.get('reason', value.get('readback', ''))}" for key, value in report["stages"].items())
        + "\n\n原始结构化结果见 `results.json`，通信原文见 `communication.jsonl`；物理输出未测。\n",
        encoding="utf-8",
    )
    print(json.dumps({"batch": str(batch), "results": str(batch / "results.json"), "summary": str(batch / "summary.json"), "stages": report["stages"]}, ensure_ascii=False, indent=2))
    return 0 if not report.get("error") else 2


if __name__ == "__main__":
    raise SystemExit(main())
