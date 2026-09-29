"""Selected-asset USB/TSERVE check; --control only tests idle service settings."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from lab_control.application.config import ConnectionConfig, load_config
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceCommand, DeviceError, jsonable, utc_now
from lab_control.instruments.common.hardware_paths import verify_runtime_paths
from lab_control.storage.repository import application_home

from ..adapter import create, is_ready
from ..discovery import enumerate_devices
from ..profile import FREQUENCIES
from .records import publish_report


def frequency_parameters(value: dict[str, Any]) -> dict[str, Any]:
    return {"automatic": True} if value["automatic"] else {"value": value["value"]}


def service_configuration_probe(runtime, config, report, trace, save, *, control):
    """Explicit maintenance after blocked identity. Never mark an instrument connected."""
    owner = "53100a-service-maintenance"
    runtime.leases.acquire(
        owner,
        (
            config.resource,
            "USBPNP::" + config.usb_instance_id,
            f"asset:{config.adapter}:{config.expected_serial}",
        ),
        identified=True,
    )
    client = None
    try:
        client = create(config, trace=trace)
        report["service_details"] = client.inspect_service()
        report.setdefault("service_logs", []).append(str(client.server.folder))
        report["stages"].update(service_connection="passed", service_state="passed")
        report["initial_status"] = jsonable(client.read_status())
        report["final_state"] = "TSERVE Ready; hardware identity remains unverified; measurement not started"

        def call(operation, argument=None):
            assert client is not None
            if operation == "read_settings":
                return client.read_settings(*argument)
            if operation == "status":
                return client.read_status()
            if operation == "apply" and argument.operation in FREQUENCIES:
                return client.apply(argument)
            raise ValueError("Only idle service configuration maintenance is allowed")

        if control:
            validate_settings(call, report, save)
        else:
            report["initial_settings"] = jsonable(client.read_settings(tuple(FREQUENCIES)))
            report["stages"]["parameter_read"] = "passed"
        client.close()
        client = None
        report["stages"]["service_shutdown"] = "passed"
        client = create(config, trace=trace)
        report["service_reconnected"] = {
            "details": client.inspect_service(),
            "settings": jsonable(client.read_settings(tuple(FREQUENCIES))),
            "status": jsonable(client.read_status()),
        }
        report["service_logs"].append(str(client.server.folder))
        report["stages"]["service_reconnection"] = "passed"
    finally:
        try:
            if client is not None:
                client.close()
        except Exception as exc:
            report["stages"]["service_shutdown"] = {"state": "failed", "reason": str(exc)}
            raise
        finally:
            runtime.leases.release(owner)


def validate_settings(call: Callable, report: dict, save: Callable[[], None]) -> None:
    """Stop on the first uncertain write; restore only from a complete fresh read."""

    def snapshot():
        return {item.operation: item.value for item in call("read_settings", (tuple(FREQUENCIES), 1))}

    initial = snapshot()
    report["initial_settings"] = initial
    report["stages"]["parameter_read"] = "passed"
    report["operations"] = []
    save()
    for name in FREQUENCIES:
        target = 20e6 if initial[name] == {"automatic": False, "value": 10e6} else 10e6
        entry: dict[str, Any] = {"operation": name, "requested_hz": target, "started_at": utc_now()}
        report["operations"].append(entry)
        report["stages"]["restoration"] = {"state": "blocked", "reason": "Setting change in progress"}
        save()
        try:
            result = call("apply", DeviceCommand(name, {"value": target}))
            entry["apply"] = jsonable(result)
            actual = snapshot()
            entry["readback"] = actual
            other = next(key for key in FREQUENCIES if key != name)
            if result.outcome != "success" or actual[name] != {"automatic": False, "value": target}:
                raise DeviceError(
                    "TSERVE configuration readback mismatch or uncertain write", "verification_failed"
                )
            if actual[other] != initial[other]:
                raise DeviceError("Other frequency changed during single-setting test", "verification_failed")
            entry["state"] = "passed"
        except Exception as exc:
            entry.update(state="failed", error=str(exc))
            report["stages"]["parameter_write"] = {"state": "failed", "reason": str(exc)}
            raise
        finally:
            # Never repeat the test write. A closed/timed-out session cannot yield
            # a trustworthy pre-restore snapshot; then leave final state unknown.
            try:
                current = snapshot()
                entry["before_restore"] = current
                entry["restore"] = []
                for key, original in initial.items():
                    if current[key] != original:
                        restored = call("apply", DeviceCommand(key, frequency_parameters(original)))
                        entry["restore"].append(jsonable(restored))
                        if restored.outcome != "success":
                            raise DeviceError(
                                "TSERVE configuration restoration not confirmed", "restoration_failed"
                            )
                final = snapshot()
                status = call("status")
                entry["final_settings"] = final
                entry["final_status"] = jsonable(status)
                report["final_snapshot"] = {"settings": final, "status": jsonable(status)}
                if final != initial or not is_ready(status.values["state"]):
                    raise DeviceError("Final configuration/state differs from backup", "restoration_failed")
                report["stages"]["restoration"] = "passed"
                report["final_state"] = (
                    "Original TSERVE settings restored and Ready read back; physical inputs not measured"
                )
            except Exception as exc:
                entry["restore_error"] = str(exc)
                report["stages"]["restoration"] = {"state": "failed", "reason": str(exc)}
                report["final_state"] = "Restoration unconfirmed; see last observed settings; no write replay"
                save()
                raise
            finally:
                entry["finished_at"] = utc_now()
                save()
    report["stages"]["parameter_write"] = {
        "state": "passed",
        "reason": "TSERVE configuration read/write only",
    }


def run(config: ConnectionConfig, *, control: bool = False) -> Path:
    if config.adapter != "microchip.53100a" or not config.expected_serial:
        raise ValueError("Select exactly one 53100A with an independently verified expected_serial")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{1,127}", config.expected_serial):
        raise ValueError("Invalid expected serial")
    root = application_home()
    lock_root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "TF-LabControl"
    paths = verify_runtime_paths(root, lock_root)
    asset = "53100A-" + re.sub(r"[^A-Za-z0-9._-]", "_", config.expected_serial)
    folder = root / "hardware-tests" / asset / (datetime.now().strftime("%Y-%m-%d-%H%M%S-%f") + "-usb-tserve")
    folder.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "adapter_id": config.adapter,
        "config": jsonable(config),
        "resource": config.resource,
        "expected_serial": config.expected_serial,
        "identity": {},
        "started_at": utc_now(),
        "scope": "USB and power only; idle TSERVE configuration check; no measurement start"
        if control
        else "Read-only USB/TSERVE connection; no measurement or parameter writes",
        "environment": {"paths": paths, "python": sys.version},
        "project_head": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parents[1].glob("*.py")
        },
        "stages": {
            key: "not_tested"
            for key in (
                "enumeration",
                "identity",
                "connection",
                "service_state",
                "parameter_read",
                "parameter_write",
                "restoration",
                "reconnection",
                "service_shutdown",
                "phase_noise",
                "am_noise",
                "stability",
                "physical_output",
            )
        },
        "final_state": "Not connected; no commands sent",
    }
    runtime = None
    device_id = ""

    def save():
        (folder / "results.json").write_text(
            json.dumps(jsonable(report), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def trace(event):
        with (folder / "communication.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps({"at": utc_now(), **event}, ensure_ascii=False) + "\n")

    def call(operation, argument=None):
        assert runtime is not None
        return runtime.sessions.submit(device_id, operation, argument).result()

    stage = "enumeration"
    save()
    try:
        rows = enumerate_devices() if config.usb_instance_id else ()
        report["environment"]["pnp"] = rows
        if config.usb_instance_id:
            selected = [row for row in rows if row["PNPDeviceID"].upper() == config.usb_instance_id.upper()]
            if len(selected) != 1 or selected[0]["ConfigManagerErrorCode"]:
                raise DeviceError("Selected USB target absent or driver not ready", "driver_missing")
            report["stages"]["enumeration"] = "passed"
        stage = "connection"
        runtime = ApplicationRuntime((config,))

        def factory(selected):
            client = create(selected, trace=trace)
            if client.server:
                report.setdefault("service_logs", []).append(str(client.server.folder))
            return client

        runtime.sessions.factory = factory
        device_id = runtime.add(config)
        identity = runtime.connect(device_id, configure_acquisition=False).result()
        report["identity"] = jsonable(identity)
        report["stages"].update(identity="passed", connection="passed", service_connection="passed")
        if identity.confidence == "usb_descriptor":
            report["stages"]["usb_identity"] = "passed"
            report["stages"]["tserve_hardware_identity"] = {
                "state": "blocked",
                "reason": "Idle TSERVE cannot report serial; identity independently read from USB",
            }
        session = runtime.sessions.sessions[device_id]
        report["service_details"] = getattr(session.adapter, "capability_details", {})
        service = getattr(session.adapter, "server", None)
        report.setdefault("service_logs", [])
        stage = "service_state"
        status = call("status")
        report["initial_status"] = jsonable(status)
        if not is_ready(status.values["state"]):
            raise DeviceError("Measurement not idle; no setting writes permitted", "task_busy")
        report["stages"][stage] = "passed"
        report["final_state"] = "TSERVE Ready; no measurement started"
        stage = "parameter_read"
        if control:
            validate_settings(call, report, save)
        else:
            report["initial_settings"] = jsonable(call("read_settings", (tuple(FREQUENCIES), 1)))
            report["stages"][stage] = "passed"
        save()
        stage = "service_shutdown"
        runtime.sessions.disconnect(device_id, drain_reads=True)
        report["stages"][stage] = "passed"
        stage = "reconnection"
        second = runtime.connect(device_id, configure_acquisition=False).result()
        report["reconnected"] = {
            "identity": jsonable(second),
            "status": jsonable(call("status")),
            "settings": jsonable(call("read_settings", (tuple(FREQUENCIES), 1))),
        }
        if second.serial != config.expected_serial:
            raise DeviceError("Reconnected identity changed", "identity_conflict")
        service = getattr(runtime.sessions.sessions[device_id].adapter, "server", None)
        if service:
            report["service_logs"].append(str(service.folder))
        report["stages"][stage] = "passed"
        report["stages"]["service_reconnection"] = "passed"
    except Exception as exc:
        report["error"] = str(exc)
        if report["stages"][stage] == "not_tested":
            report["stages"][stage] = {"state": "blocked", "reason": str(exc)}
        if getattr(exc, "code", "") in {"identity_required", "identity_conflict"}:
            report["stages"]["identity"] = {"state": "blocked", "reason": str(exc)}
        if (
            getattr(exc, "code", "") == "identity_required"
            and runtime is not None
            and config.usb_instance_id
            and device_id in runtime.sessions.sessions
        ):
            # TSERVE 1.780A does not expose serial until acquisition is running.
            # The verified-instrument session remains blocked. Inspect only an
            # owned, explicitly serial-bound server, under the same asset locks.
            try:
                runtime.sessions.disconnect(device_id)
                service_configuration_probe(runtime, config, report, trace, save, control=control)
            except Exception as service_error:
                report["service_error"] = str(service_error)
                report["stages"]["service_validation"] = {"state": "blocked", "reason": str(service_error)}
                if getattr(service_error, "code", "") in {"server_exit_abnormal", "cleanup_failed"}:
                    report["stages"]["service_shutdown"] = {"state": "failed", "reason": str(service_error)}
    finally:
        if runtime is not None:
            try:
                runtime.close()
                if report["stages"]["service_shutdown"] == "not_tested" and report.get("service_logs"):
                    report["stages"]["service_shutdown"] = "passed"
            except Exception as exc:
                report["cleanup_error"] = str(exc)
                report["stages"]["service_shutdown"] = {"state": "failed", "reason": str(exc)}
        report["finished_at"] = utc_now()
        save()
        (folder / "record.md").write_text(
            "# 53100A USB / TSERVE 测试\n\n"
            + report["scope"]
            + "\n\n"
            + "| 阶段 | 结果 |\n| --- | --- |\n"
            + "".join(
                f"| {key} | {json.dumps(value, ensure_ascii=False)} |\n"
                for key, value in report["stages"].items()
            )
            + "\n最终状态："
            + report["final_state"]
            + "\n\n参数结果仅表示 TSERVE 配置读写。相噪、AM、稳定度和物理性能未测试；不构成整机验收。"
            + "\n\n原始身份及通信见 results.json、communication.jsonl 和服务日志。\n",
            encoding="utf-8",
        )
        publish_report(folder)
    return folder


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--control", action="store_true", help="Explicitly authorized idle TSERVE configuration validation"
    )
    args = parser.parse_args()
    configurations = load_config(args.config)
    if len(configurations) != 1:
        parser.error("Use a batch-specific configuration containing exactly one selected asset")
    folder = run(configurations[0], control=args.control)
    print(folder)
    report = json.loads((folder / "results.json").read_text(encoding="utf-8"))
    return int(bool(report.get("error") or report.get("cleanup_error")))


if __name__ == "__main__":
    raise SystemExit(main())
