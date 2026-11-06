"""Explicitly remember/disable an asset's connection setup and verify it with real evidence."""

from __future__ import annotations

import argparse
import hashlib
import platform
import subprocess
import traceback
from datetime import datetime, timezone
from pathlib import Path

import pyvisa

from lab_control.application.runtime import ApplicationRuntime
from lab_control.discovery.types import DiscoveryCandidate
from lab_control.domain.connections import ConnectionConfig, network_host
from lab_control.domain.instruments import DeviceError, jsonable, utc_now
from lab_control.instruments.common.hardware_paths import verify_runtime_paths
from lab_control.storage.hardware_documents import refresh_after_batch
from lab_control.storage.repository import application_home
from lab_control.storage.runtime import hardware_lock_home

from ..adapter import M300Adapter
from ..connection_setup import validate_channels
from .records import publish_report
from .software_temperature import SoftwareTemperatureTest, parse_curve_groups
from .validate_control import RecordingTransport, save_json


def commission(config, folder, *, root, channels=None, unit="C", duration=8.0, disable=False):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / "results.json").exists():
        raise ValueError("Do not overwrite an existing hardware batch")
    report = dict(
        schema_version=1,
        adapter_id="rigol.m300",
        model="M300",
        expected_serial=config.expected_serial,
        resource=config.resource,
        started_at=utc_now(),
        scope=dict(
            action="disable" if disable else "remember_and_verify",
            channels=channels,
            unit=unit,
            duration=duration,
            retain_configured=True,
        ),
        environment=dict(
            python=platform.python_version(), pyvisa=pyvisa.__version__, platform=platform.platform()
        ),
        stages={
            key: dict(state="not_tested", reason="Not reached or outside this plan")
            for key in (
                "enumeration",
                "identity",
                "compatibility_connection",
                "parameter_read",
                "parameter_write",
                "monitoring",
                "recording",
                "stop",
                "recovery",
                "reconnect",
                "physical_output",
            )
        },
        operations=[],
        samples=[],
    )
    app, device, state_key = None, "", ""
    report["model_source_hashes"] = {
        str(path.relative_to(Path(__file__).parents[1])): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in Path(__file__).parents[1].rglob("*.py")
    }
    try:
        report["source_version"] = dict(
            commit=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            dirty=subprocess.check_output(["git", "status", "--short"], text=True),
        )
    except (OSError, subprocess.CalledProcessError):
        report["source_version"] = "Installed package; model source hashes retained"
    stage = "identity"
    try:
        if config.adapter != "rigol.m300" or not config.expected_serial:
            raise ValueError("Explicit M300 adapter and expected serial required")
        if not disable:
            validate_channels(channels, unit)
            if unit != "C" or not 1 <= duration <= 60:
                raise ValueError("This monitoring verification requires Celsius and 1..60 seconds")
        report["paths"] = verify_runtime_paths(Path(root), hardware_lock_home())
        app = ApplicationRuntime((), root=Path(root), hardware=True)
        report["environment"]["visa_library"] = str(app.visa.manager.visalib)
        factory = app.sessions.factory

        def logged(selected):
            if (selected.adapter, selected.resource, selected.expected_serial) != (
                config.adapter,
                config.resource,
                config.expected_serial,
            ):
                raise DeviceError("Only the selected M300 is authorized", "identity_conflict")
            adapter = factory(selected)
            assert isinstance(adapter, M300Adapter)
            adapter.transport = RecordingTransport(adapter.transport, folder / "communication.jsonl")
            return adapter

        app.sessions.factory = logged
        owner = "m300-connection-setup-identity"
        app.leases.acquire(owner, (config.resource, "discovery:unknown_alias"))
        try:
            probe = logged(config)
            try:
                identity = probe.identify()
                if identity.serial != config.expected_serial:
                    raise DeviceError("Selected M300 serial differs", "identity_conflict")
            finally:
                probe.close()
        finally:
            app.leases.release(owner)
        report["identity"] = jsonable(identity)
        report["stages"][stage] = dict(state="passed")
        record = app.inventory.merge(
            DiscoveryCandidate(config, "configured", safe_scpi=True),
            folder.name,
            identity,
            "rigol.m300",
            reason="identified",
        )
        device = record["id"]
        state_key = f"device-control-state/{device}"
        report["saved_profile_before"] = app.repository.load_workspace(state_key)
        report["connection_before"] = record
        save_json(folder / "progress.json", report)
        follow_address = bool(network_host(config.resource)) and (
            not disable or not record.get("primaryResourceExplicit")
        )
        app.inventory.select_resource(device, config.resource, follow_address=follow_address)
        stage = "compatibility_connection"
        app.connect(device, configure_acquisition=False).result()
        report["stages"][stage] = dict(state="passed")
        session = app.sessions.sessions[device]
        adapter = session.adapter
        assert isinstance(adapter, M300Adapter)
        stage = "parameter_read"
        before = session.worker.submit(adapter.connection_setup.snapshot).result()
        report["initial_snapshot"] = before
        save_json(folder / "initial_snapshot.json", before)
        report["stages"][stage] = dict(state="passed")
        if disable:
            session.worker.submit(adapter.disable_thermistor_setup, confirmed=True).result()
            report["stages"]["profile_registration"] = dict(state="passed", enabled=False)
            report["final_state"] = before
        else:
            test = SoftwareTemperatureTest(app, device, folder, report, channels)
            stage = "profile_registration"
            session.worker.submit(
                adapter.remember_resistance_temperature_setup, channels, unit=unit, confirmed=True
            ).result()
            report["stages"][stage] = dict(
                state="passed", enabled=True, follow_verified_address=follow_address
            )
            app.sessions.disconnect(device)
            stage = "parameter_write"
            app.connect(device).result()
            session = app.sessions.sessions[device]
            adapter = session.adapter
            assert isinstance(adapter, M300Adapter)
            report["setup_result"] = jsonable(session.acquisition_preparation)
            report["operations"] = report["setup_result"].get("operations", [])
            if report["setup_result"].get("status") == "blocked":
                raise DeviceError(report["setup_result"]["message"], "acquisition_setup_blocked")
            report["stages"][stage] = dict(state="passed", writes=len(report["operations"]))
            configured = session.worker.submit(adapter.connection_setup.snapshot).result()
            report["configured_snapshot"] = configured
            points = int(float(test.q("DATA:POIN?")))
            report["memory_backup"] = dict(
                points=points,
                raw=test.q("FETC?") if points else "",
                restoration="READ? replaces instrument reading memory; original values backed up but cannot be restored",
            )
            save_json(folder / "progress.json", report)
            stage = "monitoring"
            test.monitor(duration)
            stage = "reconnect"
            app.sessions.disconnect(device)
            identity_again = app.connect(device).result()
            session = app.sessions.sessions[device]
            adapter = session.adapter
            assert isinstance(adapter, M300Adapter)
            report["reconnect_setup"] = jsonable(session.acquisition_preparation)
            if session.acquisition_preparation.get("operations"):
                raise DeviceError(
                    "Already configured M300 unexpectedly required setup writes", "readback_mismatch"
                )
            test.capture("reconnected", 1)
            final = session.worker.submit(adapter.connection_setup.snapshot).result()
            report["final_state"] = final
            if final != configured:
                raise DeviceError("Settings changed after monitoring/reconnect", "readback_mismatch")
            report["stages"][stage] = dict(state="passed", identity=jsonable(identity_again), setup_writes=0)
            report["stages"]["recovery"] = dict(
                state="not_tested",
                reason="Authorized profile deliberately retained; idle after bounded monitoring, not reverted to the empty original scan",
            )
    except (Exception, KeyboardInterrupt) as exc:
        report["error"], report["traceback"] = str(exc), traceback.format_exc()
        saved = app.repository.load_workspace(state_key) if app and state_key else None
        attempt = (saved or {}).get("last_attempt") or {}
        preflight_blocked = attempt.get("status") == "blocked" and not attempt.get("operations")
        report["stages"][stage] = dict(
            state="blocked"
            if preflight_blocked
            or getattr(exc, "code", "") in {"process_busy", "resource_busy", "driver_missing"}
            else "failed",
            reason=str(exc),
        )
        if preflight_blocked:
            report["last_verified_snapshot"] = attempt.get("before")
            report["stages"]["recovery"] = dict(
                state="not_tested", reason="Preflight blocked; no parameter writes to restore"
            )
        report["final_state_warning"] = (
            "No automatic retry or rollback; consult saved setup attempt for last confirmed state"
        )
    finally:
        if app:
            try:
                if device:
                    app.monitoring.stop(device).result(30)
                if state_key:
                    report["saved_profile_after"] = app.repository.load_workspace(state_key)
            except Exception as exc:
                report["stop_error"] = str(exc)
            try:
                app.close()
                report["stages"]["release"] = dict(state="passed")
            except Exception as exc:
                report["close_error"] = str(exc)
                report["stages"]["release"] = dict(state="failed", reason=str(exc))
        report["finished_at"] = utc_now()
        save_json(folder / "results.json", report)
        lines = [
            "# M300 持久连接配置及监测验证",
            "",
            "按已核验的型号/序列号绑定配置，LAN 地址经身份确认后跟随。扫描和只读连接不应用配置。",
            "",
        ]
        lines += [
            f"- {key}: **{value['state']}** {value.get('reason', '')}"
            for key, value in report["stages"].items()
        ]
        lines += [
            "",
            "成功时保留已授权的温度配置和扫描列表，监测已停止。失败以 results.json 的最后核验状态为准，不声称恢复。",
            "",
            "未使用外部参考仪器验收测温准确度。原始通信见 communication.jsonl，原始/最终配置及保存策略见 results.json。",
        ]
        (folder / "record.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        publish_report(folder)
        refresh_after_batch(folder)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--resource", required=True)
    parser.add_argument("--expected-serial", required=True)
    parser.add_argument("--home", type=Path, default=application_home())
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--group", action="append", help="Authorized MC3132 range=curve ID, e.g. 401:416=mf501-v1"
    )
    selection.add_argument("--disable", action="store_true")
    parser.add_argument("--duration", type=float, default=8)
    args = parser.parse_args(argv)
    try:
        channels = parse_curve_groups(args.group) if args.group else None
    except (ValueError, DeviceError) as exc:
        parser.error(str(exc))
    folder = (
        args.home
        / "hardware-tests"
        / f"RIGOL-M300-{args.expected_serial}"
        / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-connection-setup")
    )
    report = commission(
        ConnectionConfig(
            args.resource, "rigol.m300", "visa", expected_serial=args.expected_serial, timeout=15
        ),
        folder,
        root=args.home,
        channels=channels,
        duration=args.duration,
        disable=args.disable,
    )
    print(folder)
    return 2 if report.get("error") or report.get("close_error") or report.get("stop_error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
