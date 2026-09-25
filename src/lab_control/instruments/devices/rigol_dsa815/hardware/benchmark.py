"""Explicit, locked DSA815 performance qualification. Never saves trace samples."""

from __future__ import annotations

import argparse
import hashlib
import os
import time
from dataclasses import asdict, replace
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np

from lab_control.application.config import ConnectionConfig, load_config
from lab_control.application.connection_preferences import environment_signature, preference_key
from lab_control.application.runtime import ApplicationRuntime
from lab_control.discovery.addresses import network_host
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError, jsonable, utc_now
from lab_control.instruments.base import command, enum, number, parameter
from lab_control.instruments.registry import create_adapter
from lab_control.storage.repository import application_home
from lab_control.transport.diagnostics import measured

from ..adapter import Dsa815Adapter
from ..protocol import boolean, choice
from .records import publish_report
from .state import verification_profile
from .validate_control import RecordingTransport, TestSession, differences, save_json, validate_target

EXTRA = ("display_scale", "display_spacing", "preamplifier", "transfer_format", "byte_order")


def benchmark_profile():
    profile = verification_profile()
    commands = dict(profile.commands)
    for name, path, param, decoder in (
        ("display_scale", "DISP:WIN:TRAC:Y:SCAL:PDIV", parameter("dB", 0.1, 20), number),
        (
            "display_spacing",
            "DISP:WIN:TRAC:Y:SCAL:SPAC",
            enum("LIN", "LOG"),
            partial(choice, choices=("LIN", "LOG")),
        ),
        ("preamplifier", "SENS:POW:RF:GAIN:STAT", None, boolean),
        ("transfer_format", "FORM", enum("ASC", "REAL"), partial(choice, choices=("ASC", "REAL"))),
        ("byte_order", "FORM:BORD", enum("NORM", "SWAP"), partial(choice, choices=("NORM", "SWAP"))),
    ):
        if name == "preamplifier":
            from lab_control.domain.instruments import Parameter

            param = Parameter("boolean")
        commands[name] = replace(command(name, path, param, scoped=False), decode=decoder, compare=False)
    return replace(profile, commands=commands)


class BenchmarkTransport(RecordingTransport):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.profile = benchmark_profile()

    @measured("query")
    def query(self, payload: bytes | str) -> bytes:
        # RecordingTransport sends via inner.write/read, so account query here once.
        return super().query(payload)


def summarize_frames(frames: list[dict[str, Any]]) -> dict[str, Any]:
    values = [f["performance"]["acquisition_seconds"] for f in frames]
    return {
        "frames": len(frames),
        "median": float(np.median(values)),
        "p95": float(np.percentile(values, 95)),
        "maximum": max(values),
        "live_frames": sum(f["validation"] == "live" for f in frames),
        "full_frames": sum(f["validation"] == "full" for f in frames),
        "cached_frames": sum(f["validation"] == "cached" for f in frames),
    }


def qualifies(
    baseline: dict, candidate: dict, stages: dict, *, production_strategy_differs: bool = False
) -> bool:
    required = {"connection", "backup"} | {
        f"{transport}_{group}"
        for transport in ("vxi11", "socket")
        for group in (
            "setup",
            "ASC_full",
            "ASC_live",
            "REAL_full",
            "REAL_live",
            "zero_span_full",
            "zero_span_live",
            "auto",
            "restoration",
            "reconnection",
        )
    }
    return bool(
        not production_strategy_differs
        and baseline.get("frames", 0) >= 20
        and candidate.get("frames", 0) >= 20
        and candidate["median"] <= baseline["median"] * 0.8
        and candidate["p95"] <= baseline["p95"]
        and required <= stages.keys()
        and all(value == "passed" for key, value in stages.items() if key != "physical_output")
    )


def frame_record(batch) -> dict[str, Any]:
    if not batch.measurements or not batch.measurements[0].metadata.get("sha256"):
        raise DeviceError("No qualified trace", "acquisition_failed")
    m = batch.measurements[0]
    return {
        "timestamp": batch.timestamp,
        "validation": batch.validation,
        "settings_timestamps": batch.settings_timestamps,
        "performance": batch.performance,
        "unit": m.unit,
        "axis_unit": m.axis_unit,
        "axis_start": float(m.axis[0]),
        "axis_stop": float(m.axis[-1]),
        **m.metadata,
    }


def run(
    config: ConnectionConfig, folder: Path, *, control: bool = False, compare_queries: bool = False
) -> dict:
    folder.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "adapter_id": config.adapter,
        "resource": config.resource,
        "config": asdict(config),
        "expected_serial": config.expected_serial,
        "started_at": utc_now(),
        "scope": "DSA815 performance; passive traces; no samples saved; RF input empty or 50 ohm",
        "compare_queries": compare_queries,
        "environment": {},
        "operations": [],
        "traces": [],
        "groups": {},
        "stages": {"connection": "not_tested", "physical_output": "not_tested"},
        "active_stage": "connection",
        "final_state": "No setting writes performed",
        "source_sha256": {
            str(p.relative_to(Path(__file__).parents[1])): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path(__file__).parents[1].rglob("*.py")
            if "tests" not in p.parts
        },
    }
    app = test = original = extras = None
    mutated = False
    restoring = False
    selected = config
    key = ""
    query_strategy = "production"

    def stage(name: str) -> None:
        report["active_stage"] = name
        report["stages"][name] = "not_tested"
        save_json(folder / "results.json", report)

    def done() -> None:
        report["stages"][report["active_stage"]] = "passed"
        save_json(folder / "results.json", report)

    try:
        if control and compare_queries:
            raise DeviceError("Query comparison is read-only and cannot combine with control", "unsupported")
        home = application_home()
        lock = Path(os.environ["LOCALAPPDATA"]) / "TF-LabControl/hardware.lock"
        if any(p.resolve() != p for p in (home, home / "inventory.sqlite3", lock, folder)):
            raise DeviceError("Native formal runtime paths required", "path_redirected")
        if not folder.is_relative_to(home / "hardware-tests"):
            raise DeviceError("Formal hardware-test folder required", "path_redirected")
        if (
            config.adapter != "rigol.dsa815"
            or config.transport != "visa"
            or not config.expected_serial
            or not network_host(config.resource)
        ):
            raise DeviceError("Exact selected LAN DSA815 required", "identity_required")
        app = ApplicationRuntime((config,))
        report["environment"] = {**environment_signature(app.visa), "home": str(home), "lock": str(lock)}
        report["enumeration"] = app.visa.discover_lan()

        class BenchmarkAdapter(Dsa815Adapter):
            def grouped_frame_queries(self) -> bool:
                # Protocol comparison must use identical scalar sequences on
                # both transports. Query comparison is an independent axis.
                return not control and query_strategy != "separate" and super().grouped_frame_queries()

        def factory(candidate):
            if candidate != selected:
                raise DeviceError("Different asset requested", "identity_conflict")
            adapter = create_adapter(candidate, app.visa)
            if not isinstance(adapter, Dsa815Adapter):
                raise DeviceError("Wrong model adapter", "identity_conflict")
            adapter = BenchmarkAdapter(adapter.transport, benchmark_profile(), candidate)
            adapter.transport = BenchmarkTransport(adapter.transport, folder / "communication.jsonl", control)
            return adapter

        app.sessions.factory = factory
        key = app.add(config)
        identity = app.sessions.connect(key, selected, configure_acquisition=False).result(65)
        report.update(identity=jsonable(identity), inventory_id=key)
        test = TestSession(app, key, report, folder)
        done()
        stage("backup")
        original = test.snapshot()
        extras = {op: test.read(op) for op in EXTRA}
        report.update(initial_snapshot=original, extra_snapshot=extras)
        save_json(folder / "initial_snapshot.json", {"settings": original, "extra": extras})
        report["initial_snapshot_sha256"] = hashlib.sha256(
            (folder / "initial_snapshot.json").read_bytes()
        ).hexdigest()
        done()

        def frames(name: str, consistency: str, count: int = 20) -> None:
            stage(name)
            entries = []
            for _ in range(count):
                batch = app.sessions.submit(
                    key, "acquire", AcquisitionRequest("spectrum", consistency=consistency)
                ).result(120)
                entry = frame_record(batch)
                entries.append(entry)
                report["traces"].append({"group": name, **entry})
                save_json(folder / "results.json", report)
            report["groups"][name] = summarize_frames(entries)
            done()

        if not control:
            if compare_queries:
                target_adapter = app.sessions.sessions[key].adapter
                assert isinstance(target_adapter, Dsa815Adapter)
                if not target_adapter.grouped_frame_queries():
                    raise DeviceError(
                        "Grouped queries are not qualified for this firmware/transport", "unsupported"
                    )
                for query_strategy in ("separate", "grouped"):
                    target_adapter.invalidate_acquisition_context("benchmark_strategy_change")
                    frames(query_strategy + "_full", "full")
                    frames(query_strategy + "_live", "live")
                stage("grouped_monitoring")
                app.monitoring.start(key, (AcquisitionRequest("spectrum"),), interval=0.05)
                started = time.monotonic()
                sequence = 0
                while time.monotonic() - started < 60:
                    task = app.monitoring.snapshots()[key]
                    if task.get("error") or not task["running"] or task["recording"]:
                        raise DeviceError(
                            "Passive monitor stopped or unexpectedly recorded", "acquisition_failed"
                        )
                    frame = app.monitoring.latest(key, "spectrum")
                    if frame and frame.sequence != sequence:
                        sequence = frame.sequence
                        report["traces"].append({"group": "grouped_monitoring", **frame_record(frame.batch)})
                        save_json(folder / "results.json", report)
                    time.sleep(0.1)
                app.monitoring.stop(key).result(120)
                if sequence < 2:
                    raise DeviceError("Insufficient monitoring frames", "acquisition_failed")
                done()
            else:
                frames("vxi11_existing_full", "full")
                frames("vxi11_existing_live", "live")
            stage("unchanged_settings")
            report["final_snapshot"] = test.snapshot()
            report["restoration_differences"] = differences(report["final_snapshot"], original)
            report["extra_differences"] = {
                op: {"expected": extras[op], "actual": value}
                for op in EXTRA
                if (value := test.read(op)) != extras[op]
            }
            if report["restoration_differences"] or report["extra_differences"]:
                raise DeviceError("Settings changed during passive benchmark", "readback_mismatch")
            done()
            if compare_queries:
                stage("reconnection")
                report["reconnections"] = []
                for _ in range(3):
                    app.sessions.disconnect(key)
                    reopened = app.sessions.connect(key, selected, configure_acquisition=False).result(65)
                    final = test.snapshot()
                    delta = differences(final, original)
                    extra = {op: test.read(op) for op in EXTRA}
                    report["reconnections"].append(
                        {"identity": jsonable(reopened), "differences": delta, "extra": extra}
                    )
                    if reopened != identity or delta or extra != extras:
                        raise DeviceError("Reconnection state differs", "readback_mismatch")
                done()
            report["final_state"] = "Read-only benchmark; no setting writes or sample exports"
            return report

        validate_target(original, config)
        target_adapter = app.sessions.sessions[key].adapter
        assert isinstance(target_adapter, Dsa815Adapter)
        for op in EXTRA:
            target_adapter.validate_command(DeviceCommand(op, {"value": extras[op]}))
        if differences(test.snapshot(), original) or {op: test.read(op) for op in EXTRA} != extras:
            raise DeviceError("Backup changed before writing", "backup_unreliable")
        host = network_host(config.resource)
        candidates = {
            "vxi11": replace(config, resource=f"TCPIP0::{host}::inst0::INSTR"),
            "socket": replace(config, resource=f"TCPIP0::{host}::5555::SOCKET"),
        }
        for transport_name, candidate in candidates.items():
            stage(f"{transport_name}_preflight")
            if candidate != selected:
                app.sessions.disconnect(key)
                selected = candidate
                reopened = app.sessions.connect(key, selected, configure_acquisition=False).result(65)
                if reopened != identity:
                    raise DeviceError("Reopened identity differs", "identity_conflict")
            if differences(test.snapshot(), original) or {op: test.read(op) for op in EXTRA} != extras:
                raise DeviceError("Candidate backup differs before setting writes", "backup_unreliable")
            # Completeness must be proved for this transport, not inherited from
            # another resource with different message-boundary semantics.
            if test.read("options") != original["options"]:
                raise DeviceError("Candidate options backup unavailable", "backup_unreliable")
            done()
            stage(f"{transport_name}_setup")
            mutated = True
            report.pop("final_snapshot", None)
            report.pop("restoration_differences", None)
            report["final_state"] = "Test settings applied; restoration not yet verified"
            save_json(folder / "restoration-differences.json", {"status": "not_verified"})
            test.apply("continuous", False)
            test.apply("span", 0)
            test.apply("center_frequency", 100e6)
            test.apply("span", 2e6)
            test.apply("reference_level", -20)
            for op, value in (("rbw", 10000), ("vbw", 3000), ("attenuation", 20)):
                test.apply(op + "_auto", False)
                test.apply(op, value)
            test.apply("sweep_time_auto", True)
            test.apply("detector", "NORM")
            test.apply("trace_mode", "WRIT")
            test.apply("continuous", True)
            done()
            for fmt in ("ASC", "REAL"):
                test.apply("transfer_format", fmt)
                frames(f"{transport_name}_{fmt}_full", "full")
                frames(f"{transport_name}_{fmt}_live", "live")
            test.apply("transfer_format", extras["transfer_format"])
            test.apply("span", 0)
            test.apply("sweep_time_auto", False)
            test.apply("sweep_time", 0.1)
            frames(f"{transport_name}_zero_span_full", "full")
            frames(f"{transport_name}_zero_span_live", "live")
            test.apply("span", 2e6)
            stage(f"{transport_name}_auto")
            test.apply("auto_setup")
            # Use the real shared transition policy, with a 30 s settling deadline.
            app.monitoring.start(key, (AcquisitionRequest("spectrum"),), interval=0.05)
            started = time.monotonic()
            sequence = 0
            while time.monotonic() - started < 60:
                task = app.monitoring.snapshots()[key]
                if task.get("error") or not task["running"]:
                    raise DeviceError(task.get("error", "Monitoring stopped"), "acquisition_failed")
                frame = app.monitoring.latest(key, "spectrum")
                if frame and frame.sequence != sequence:
                    sequence = frame.sequence
                    report["traces"].append(
                        {"group": f"{transport_name}_monitor", **frame_record(frame.batch)}
                    )
                    save_json(folder / "results.json", report)
                time.sleep(0.1)
            app.monitoring.stop(key).result(120)
            if sequence < 2:
                raise DeviceError("Insufficient stable monitoring frames", "acquisition_failed")
            done()
            stage(f"{transport_name}_restoration")
            restoring = True
            for op in EXTRA:
                test.apply(op, extras[op])
            test.restore(original)
            if {op: test.read(op) for op in EXTRA} != extras:
                raise DeviceError("Extra settings restoration differs", "restore_failed")
            restoring = False
            mutated = False
            report.setdefault("restorations", {})[transport_name] = {
                "snapshot": report["final_snapshot"],
                "differences": report["restoration_differences"],
                "extras": extras,
            }
            report["final_state"] = "Saved settings restored and verified"
            done()
            stage(f"{transport_name}_reconnection")
            for _ in range(3):
                app.sessions.disconnect(key)
                reopened = app.sessions.connect(key, selected, configure_acquisition=False).result(65)
                final = test.snapshot()
                if (
                    reopened != identity
                    or differences(final, original)
                    or {op: test.read(op) for op in EXTRA} != extras
                ):
                    raise DeviceError("Reconnection snapshot differs", "readback_mismatch")
            done()
        mutated = False
        report["final_state"] = (
            "Settings and extra display/format state restored and verified; no samples saved"
        )
        baseline = report["groups"]["vxi11_ASC_full"]
        candidate = report["groups"]["socket_ASC_full"]
        report["socket_qualified"] = qualifies(
            baseline,
            candidate,
            report["stages"],
            production_strategy_differs=Dsa815Adapter.grouped_frame_queries(target_adapter),
        )
        if Dsa815Adapter.grouped_frame_queries(target_adapter):
            report["preference_blocked_reason"] = (
                "Identical scalar protocol comparison does not compare the faster grouped VXI-11 "
                "production strategy; no transport preference is issued"
            )
        if report["socket_qualified"]:
            app.repository.save_workspace(
                preference_key(config.adapter, identity.serial),
                {
                    "passed": True,
                    "serial": identity.serial,
                    "firmware": identity.firmware,
                    "port": 5555,
                    "adapter_revision": Dsa815Adapter.acquisition_revision,
                    "environment": environment_signature(app.visa),
                    "evidence": str(folder),
                    "baseline": baseline,
                    "candidate": candidate,
                },
            )
    except Exception as exc:
        code = getattr(exc, "code", type(exc).__name__)
        report.update(error=str(exc), error_code=code)
        report["stages"][report["active_stage"]] = (
            "blocked"
            if code
            in {"process_busy", "resource_busy", "path_redirected", "backup_unreliable", "unsupported"}
            else "failed"
        )
    finally:
        if app and key:
            app.monitoring.stop(key).result(120)
        if restoring:
            report["final_state"] = "Restoration failed; no further setting writes attempted"
        if mutated and not restoring and app and test and original and extras:
            try:
                session = app.sessions.sessions[key]
                if session.state in {"degraded", "error"}:
                    app.sessions.disconnect(key)
                    reopened = app.sessions.connect(key, selected, configure_acquisition=False).result(65)
                    if jsonable(reopened) != report["identity"]:
                        raise DeviceError("Recovery identity differs", "identity_conflict")
                    report["recovery_connection"] = "one identity-checked connection"
                report["before_restore"] = test.snapshot()
                for op in EXTRA:
                    test.apply(op, extras[op])
                test.restore(original)
                if {op: test.read(op) for op in EXTRA} != extras:
                    raise DeviceError("Extra restore differs", "restore_failed")
                report["stages"]["recovery"] = "passed"
                report["final_state"] = "Stopped on error; saved settings restored and verified"
            except Exception as exc:
                report["stages"]["recovery"] = "failed"
                report["final_state"] = "Restoration failed; stop further writes: " + str(exc)
        if app:
            app.close()
        report["finished_at"] = utc_now()
        save_json(folder / "results.json", report)
        if not (folder / "restoration-differences.json").exists():
            save_json(
                folder / "restoration-differences.json",
                report.get("restoration_differences", {"status": "not_tested"}),
            )
        publish_report(folder)
        rows = "\n".join(f"| {key} | {value} |" for key, value in report["stages"].items())
        (folder / "record.md").write_text(
            "# DSA815 通信与采集性能验收\n\n| 分组 | 结果 |\n| --- | --- |\n"
            + rows
            + "\n\n"
            + report["final_state"]
            + "\n\n"
            + report.get("error", "")
            + "\n\n轨迹只记录长度、参数和哈希；没有保存完整曲线。物理精度未验收。\n"
            + "\n[结果](results.json) · [摘要](summary.json)\n",
            encoding="utf-8",
        )
        from lab_control.storage.hardware_documents import refresh_after_batch

        refresh_after_batch(folder)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--control", action="store_true")
    mode.add_argument("--compare-queries", action="store_true")
    args = parser.parse_args()
    configs = load_config(args.config)
    if len(configs) != 1:
        parser.error("Exactly one explicit asset required")
    config = configs[0]
    folder = (
        application_home()
        / "hardware-tests"
        / ("DSA815-" + config.expected_serial)
        / (datetime.now().strftime("%Y-%m-%d-%H%M%S-%f") + "-performance")
    )
    report = run(config, folder, control=args.control, compare_queries=args.compare_queries)
    print(folder)
    return int("error" in report)


if __name__ == "__main__":
    raise SystemExit(main())
