"""Explicit MC3132 thermistor acceptance, using the shared locks and recorder."""

from __future__ import annotations

import argparse
import hashlib
import platform
import subprocess
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyvisa

from lab_control.application.runtime import ApplicationRuntime
from lab_control.discovery.types import DiscoveryCandidate
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import DeviceError, jsonable, utc_now
from lab_control.instruments.common.hardware_paths import verify_runtime_paths
from lab_control.storage.hardware_documents import refresh_after_batch
from lab_control.storage.repository import application_home
from lab_control.storage.runtime import hardware_lock_home

from ..adapter import M300Adapter
from .records import publish_report
from .validate_control import RecordingTransport, save_json


def verify_readonly(config, folder, *, duration=0, root=None, input_condition="unspecified"):
    return verify(config, Path(folder), root=root, input_condition=input_condition)


def verify(
    config: ConnectionConfig,
    folder: Path,
    *,
    root=None,
    channel: int | None = None,
    channel_nominals: dict[int, int] | None = None,
    nominal=10000,
    duration=8.0,
    leave_configured=False,
    input_condition="thermistor; curve unverified",
):
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / "results.json").exists():
        raise ValueError("Do not overwrite an existing batch")
    report: dict[str, Any] = dict(
        schema_version=1,
        adapter_id="rigol.m300",
        model="M300",
        expected_serial=config.expected_serial,
        resource=config.resource,
        interface="LAN" if config.resource.startswith("TCPIP") else "USB",
        started_at=utc_now(),
        operator="user-authorized project hardware entry",
        scope=dict(
            channel=channel,
            channel_nominals=channel_nominals,
            nominal_ohm=nominal,
            duration=duration,
            leave_configured=leave_configured,
            input_condition=input_condition,
        ),
        environment=dict(
            python=platform.python_version(), pyvisa=pyvisa.__version__, platform=platform.platform()
        ),
        stages={
            key: {"state": "not_tested"}
            for key in (
                "enumeration",
                "identity",
                "compatibility_connection",
                "parameter_read",
                "parameter_control",
                "resistance",
                "temperature",
                "monitoring",
                "recording",
                "stop",
                "recovery",
                "reconnect",
                "commissioning",
                "physical_output",
            )
        },
        operations=[],
        samples=[],
    )
    try:
        report["source_version"] = dict(
            commit=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            dirty=subprocess.check_output(["git", "status", "--short"], text=True),
        )
    except (OSError, subprocess.CalledProcessError):
        report["source_version"] = "unavailable; model source hashes retained"
    report["model_source_hashes"] = {
        str(p.relative_to(Path(__file__).parents[1])): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in Path(__file__).parents[1].rglob("*.py")
    }
    app, device = None, ""
    stage = "identity"
    try:
        if config.adapter != "rigol.m300" or not config.expected_serial:
            raise ValueError("Explicit M300 adapter and expected serial required")
        if channel is not None or channel_nominals is not None or leave_configured:
            raise ValueError("仪器直读温度测试已停用；请使用 hardware.connection_setup 并指定曲线 ID")
        home = Path(root) if root else application_home()
        report["paths"] = verify_runtime_paths(home, hardware_lock_home())
        app = ApplicationRuntime((), root=home, hardware=True)
        report["environment"]["visa_library"] = str(app.visa.manager.visalib)
        factory = app.sessions.factory

        def logged(selected):
            if (selected.adapter, selected.resource, selected.expected_serial) != (
                config.adapter,
                config.resource,
                config.expected_serial,
            ):
                raise DeviceError("Only the selected device is authorized", "identity_conflict")
            adapter = factory(selected)
            assert isinstance(adapter, M300Adapter)
            adapter.transport = RecordingTransport(adapter.transport, folder / "communication.jsonl")
            return adapter

        app.sessions.factory = logged
        try:
            advertisements = (
                app.visa.discover_lan(timeout=2)
                if config.resource.startswith("TCPIP")
                else app.visa.enumerate()
            )
            report["stages"]["enumeration"] = dict(
                state="passed" if config.resource in advertisements else "not_tested",
                advertisements=advertisements,
            )
        except Exception as exc:
            report["stages"]["enumeration"] = dict(state="blocked", reason=str(exc))
        owner = "m300-acceptance-identity"
        app.leases.acquire(owner, (config.resource, "discovery:unknown_alias"))
        try:
            probe = logged(config)
            try:
                identity = probe.identify()
                if identity.serial != config.expected_serial:
                    raise DeviceError("Selected serial differs", "identity_conflict")
            finally:
                probe.close()
        finally:
            app.leases.release(owner)
        report["identity"] = jsonable(identity)
        report["stages"]["identity"] = dict(state="passed")
        stage = "compatibility_connection"
        row = app.inventory.merge(
            DiscoveryCandidate(
                config, "lan" if config.resource.startswith("TCPIP") else "visa", safe_scpi=True
            ),
            folder.name,
            identity,
            "rigol.m300",
            reason="identified",
        )
        device = row["id"]
        app.inventory.select_resource(device, config.resource, follow_address=True)
        app.connect(device, configure_acquisition=False).result()
        report["stages"][stage] = dict(state="passed")
        report["initial_status"] = jsonable(app.sessions.submit(device, "status").result())
        report["stages"]["parameter_read"] = dict(
            state="passed" if not report["initial_status"]["errors"] else "failed"
        )
    except (Exception, KeyboardInterrupt) as exc:
        report["error"], report["traceback"] = str(exc) or type(exc).__name__, traceback.format_exc()
        blocked = isinstance(exc, DeviceError) and exc.code in {
            "process_busy",
            "resource_busy",
            "driver_missing",
            "not_configured",
        }
        report["stages"][stage] = dict(state="blocked" if blocked else "failed", reason=str(exc))
    finally:
        if app and device:
            finalization_stage = "recovery"
            try:
                try:
                    app.monitoring.stop(device).result(timeout=30)
                except Exception as exc:
                    report["stop_error"] = str(exc)
                    report["stages"]["stop"] = dict(state="failed", reason=str(exc))
                finalization_stage = "reconnect"
                app.sessions.disconnect(device)
                identity = app.connect(device, configure_acquisition=False).result()
                again = jsonable(app.sessions.submit(device, "status").result())
                matched = not again["errors"]
                report["reconnected_snapshot"] = again
                report["stages"]["reconnect"] = dict(
                    state="passed" if matched else "failed",
                    identity=jsonable(identity),
                    settings_match=matched,
                )
            except Exception as exc:
                report["finalization_error"] = str(exc)
                report["final_state_warning"] = "Final settings not fully verified; inspect raw transcript"
                report["stages"][finalization_stage] = dict(state="failed", reason=str(exc))
        if app:
            try:
                app.close()
            except Exception as exc:
                report["close_error"] = str(exc)
        report["finished_at"] = utc_now()
        report["stages"]["physical_output"] = dict(
            state="not_tested",
            reason="Read-only connection does not verify measurement accuracy",
        )
        save_json(folder / "results.json", report)
        (folder / "record.md").write_text(
            "# M300 只读接入验证\n\n仅核验身份、状态及重连，不启动测量，不验收测温精度。\n\n"
            + "\n".join(
                f"- {key}: **{value['state']}** {value.get('reason', '')}"
                for key, value in report["stages"].items()
            )
            + "\n\n参数、逐帧数据、恢复与最终配置见 results.json；原始通信见 communication.jsonl。\n",
            encoding="utf-8",
        )
        publish_report(folder)
        refresh_after_batch(folder)
    return report


def main(argv=None, *, readonly=False) -> int:
    if not readonly:
        from .connection_setup import main as commission_main

        return commission_main(argv)
    parser = argparse.ArgumentParser()
    parser.add_argument("--resource", required=True)
    parser.add_argument("--expected-serial", required=True)
    parser.add_argument("--home", type=Path, default=application_home())
    args = parser.parse_args(argv)
    folder = (
        args.home
        / "hardware-tests"
        / f"RIGOL-M300-{args.expected_serial}"
        / (datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f") + "-readonly")
    )
    report = verify(
        ConnectionConfig(
            args.resource, "rigol.m300", "visa", expected_serial=args.expected_serial, timeout=15
        ),
        folder,
        root=args.home,
    )
    print(folder)
    return (
        int(
            bool(
                report.get("error")
                or report.get("finalization_error")
                or report.get("close_error")
                or any(s["state"] == "failed" for s in report["stages"].values())
            )
        )
        * 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
