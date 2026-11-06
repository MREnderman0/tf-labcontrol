"""Opt-in, output-OFF basic-control qualification through the formal runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import re
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pyvisa

from lab_control.application.config import ConnectionConfig, load_config
from lab_control.application.runtime import ApplicationRuntime
from lab_control.discovery.service import ConfiguredDiscoveryProvider
from lab_control.domain.instruments import DeviceCommand, DeviceError, jsonable, utc_now
from lab_control.instruments.common.hardware_paths import verify_runtime_paths
from lab_control.instruments.common.readonly_hardware import QueryOnlyTransport, asset_folder_name
from lab_control.instruments.registry import create_adapter
from lab_control.storage.hardware_documents import refresh_after_batch
from lab_control.storage.repository import application_home
from lab_control.storage.runtime import hardware_lock_home

from .. import DEFINITION
from ..adapter import Sdg6032xeAdapter
from ..protocol import basic, basic_value, output
from .records import publish_report

QUERIES = (
    "*IDN?",
    "COUP?",
    "MODE?",
    "ROSC?",
    *tuple(f"C{ch}:{group}?" for ch in (1, 2) for group in ("OUTP", "BSWV", "MDWV", "SWWV", "BTWV")),
)


class TestTransport(QueryOnlyTransport):
    """Only the currently planned single parameter write is permitted."""

    permitted_write: str | None = None

    def write(self, payload: bytes | str) -> None:
        if isinstance(payload, str) and payload == self.permitted_write:
            self.permitted_write = None  # Consume permission before I/O; no replay.
            self.record("tx", payload)
            self.inner.write(payload)
            return
        super().write(payload)


def compare_snapshots(expected: dict, actual: dict) -> list[str]:
    # Full raw snapshots compare all observed fields, including output/modes,
    # derived levels, other channel and the device-wide clock/coupling state.
    return [key for key in sorted(expected.keys() | actual.keys()) if expected.get(key) != actual.get(key)]


def verify(config: ConnectionConfig, folder: Path) -> dict:
    if config.adapter != DEFINITION.adapter_id or config.transport != "visa" or not config.expected_serial:
        raise ValueError("Require one serial-bound SDG6032X-E VISA configuration")
    if not re.fullmatch(r"TCPIP\d*::[^:*?\s]+(?:::[^:*?\s]+)?::INSTR", config.resource, re.I):
        raise ValueError("This acceptance plan qualifies explicit LAN INSTR resources only")
    folder.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "started_at": utc_now(),
        "adapter_id": config.adapter,
        "resource": config.resource,
        "expected_serial": config.expected_serial,
        "scope": "Two-channel sine frequency/amplitude/offset/phase write-read-restore; outputs OFF throughout; three reconnects; no physical measurement",
        "environment": {"python": platform.python_version(), "pyvisa": pyvisa.__version__},
        "source_sha256": {
            str(path.relative_to(DEFINITION.root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in DEFINITION.root.rglob("*.py")
            if "tests" not in path.parts
        },
        "stages": {
            key: "not_tested"
            for key in (
                "scan",
                "identity",
                "connection",
                "parameter_read",
                "parameter_write",
                "channel_isolation",
                "restoration",
                "reconnection",
                "physical_output",
            )
        },
        "operations": [],
        "reconnections": [],
        "restoration_differences": [],
    }
    (folder / "plan.json").write_text(
        json.dumps(
            {
                **report,
                "test_values": {
                    "frequency": [1234, 2345],
                    "amplitude": [2, 3],
                    "offset": [0.1, -0.1],
                    "phase": [30, 60],
                },
                "stop_conditions": "Any timeout, mismatch, mode/output change stops further test writes. Reconnect explicitly before recovery if the session has failed. Restore only the current changed parameter, once, after reading actual state.",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    app = None
    device_id = ""
    pending: tuple[int, str, float] | None = None
    restore_attempted = False
    stage = "connection"
    initial: dict = {}
    wrappers: list[TestTransport] = []
    try:
        app = ApplicationRuntime((config,))
        report["runtime_paths"] = verify_runtime_paths(app.repository.root, hardware_lock_home())
        runtime = app

        def factory(selected):
            if selected != config:
                raise DeviceError("Acceptance only permits the selected asset", "identity_conflict")
            adapter = cast(Sdg6032xeAdapter, create_adapter(selected, runtime.visa))
            wrapper = TestTransport(adapter.transport, set(QUERIES), folder / "communication.jsonl")
            wrappers.append(wrapper)
            adapter.transport = wrapper
            return adapter

        def probe(candidate):
            adapter = factory(candidate.config)
            try:
                return adapter.identify(), config.adapter
            finally:
                adapter.close()

        app.sessions.factory = factory
        app.discovery.probe = probe
        app.discovery.providers = (ConfiguredDiscoveryProvider((config,)),)
        stage = "scan"
        report["scan"] = app.discovery.scan(preserve_presence=True).result(timeout=60)
        if any(item.get("error") for item in report["scan"]["items"]):
            raise DeviceError("Configured identity probe failed", "connection_failed")
        report["stages"][stage] = "passed"
        device_id = app.add(config)
        stage = "connection"
        identity = app.connect(device_id, configure_acquisition=False).result(timeout=60)
        report.update(identity=jsonable(identity), device_id=device_id)
        report["environment"]["visa"] = str(app.visa.manager.visalib)
        report["stages"].update(identity="passed", connection="passed")

        def snapshot():
            session = runtime.sessions.sessions[device_id]
            adapter = cast(Sdg6032xeAdapter, session.adapter)
            return session.worker.submit(
                lambda: {query: adapter.transport.query(query).decode("ascii").strip() for query in QUERIES}
            ).result(timeout=60)

        def write(ch, name, value):
            field = {"frequency": "FRQ", "amplitude": "AMP", "offset": "OFST", "phase": "PHSE"}[name]
            adapter = cast(Sdg6032xeAdapter, runtime.sessions.sessions[device_id].adapter)
            wrapper = cast(TestTransport, adapter.transport)
            wrapper.permitted_write = f"C{ch}:BSWV {field},{value:.12g}"
            try:
                result = runtime.sessions.submit(
                    device_id, "apply", DeviceCommand(name, {"value": float(value)}, channel=ch)
                ).result(timeout=60)
            finally:
                wrapper.permitted_write = None
            if result.outcome != "success":
                raise DeviceError(f"{name}: {jsonable(result)}", "readback_mismatch")
            return jsonable(result)

        stage = "parameter_read"
        report["initial_status"] = jsonable(app.sessions.submit(device_id, "status").result(timeout=60))
        initial = snapshot()
        report["initial_snapshot"] = initial
        backup = folder / "initial_snapshot.json"
        backup.write_text(json.dumps(initial, ensure_ascii=False, indent=2), encoding="utf-8")
        report["backup_sha256"] = hashlib.sha256(backup.read_bytes()).hexdigest()
        report["stages"][stage] = "passed"
        for ch in (1, 2):
            if (
                output(initial[f"C{ch}:OUTP?"].encode(), ch)[0] != "OFF"
                or basic(initial[f"C{ch}:BSWV?"].encode(), ch)["WVTP"] != "SINE"
            ):
                raise DeviceError(
                    "Control plan requires both outputs OFF and both channels SINE; no settings changed",
                    "test_precondition",
                )
        stage = "parameter_write"
        for ch in (1, 2):
            values = basic(initial[f"C{ch}:BSWV?"].encode(), ch)
            for name, requested in (
                ("frequency", 1234 if ch == 1 else 2345),
                ("amplitude", 2 if ch == 1 else 3),
                ("offset", 0.1 if ch == 1 else -0.1),
                ("phase", 30 if ch == 1 else 60),
            ):
                if compare_snapshots(initial, snapshot()):
                    raise DeviceError("State changed before planned operation", "configuration_changed")
                original = basic_value(values, name)
                if math.isclose(original, requested, rel_tol=0, abs_tol=1e-9):
                    raise DeviceError("Test request must differ from original", "test_precondition")
                entry = {
                    "channel": ch,
                    "operation": name,
                    "original": original,
                    "requested": requested,
                    "started_at": utc_now(),
                }
                report["operations"].append(entry)
                pending = ch, name, original
                restore_attempted = False
                entry["apply"] = write(ch, name, requested)
                actual = snapshot()
                entry["after_write"] = actual
                other = f"C{3 - ch}:"
                unaffected = {
                    k: v
                    for k, v in initial.items()
                    if k.startswith(other)
                    or k in {"COUP?", "MODE?", "ROSC?"}
                    or k.endswith(("OUTP?", "MDWV?", "SWWV?", "BTWV?"))
                }
                differences = compare_snapshots(unaffected, {k: actual[k] for k in unaffected})
                if differences:
                    raise DeviceError(
                        f"Channel/output/mode isolation failed: {differences}", "readback_mismatch"
                    )
                entry["channel_isolation"] = "passed"
                restore_attempted = True
                entry["restore"] = write(ch, name, original)
                restored = snapshot()
                entry["restored_snapshot"] = restored
                differences = compare_snapshots(initial, restored)
                if differences:
                    report["restoration_differences"] = differences
                    raise DeviceError(f"Restoration differs: {differences}", "restoration_failed")
                pending = None
                entry["restoration"] = "passed"
                entry["finished_at"] = utc_now()
        report["stages"].update(parameter_write="passed", channel_isolation="passed", restoration="passed")
        stage = "reconnection"
        for attempt in range(3):
            app.sessions.disconnect(device_id)
            actual_identity = app.connect(device_id, configure_acquisition=False).result(timeout=60)
            actual = snapshot()
            differences = compare_snapshots(initial, actual)
            report["reconnections"].append(
                {
                    "attempt": attempt + 1,
                    "identity": jsonable(actual_identity),
                    "snapshot": actual,
                    "differences": differences,
                }
            )
            if actual_identity != identity or differences:
                raise DeviceError("Reconnect identity/state mismatch", "readback_mismatch")
        report["stages"][stage] = "passed"
        report["final_snapshot"] = snapshot()
        app.sessions.disconnect(device_id)
        app.select_resource(device_id, config.resource, follow_address=True)
        report["default_lan_resource"] = config.resource
    except Exception as exc:
        report.update(error=str(exc), error_code=getattr(exc, "code", type(exc).__name__))
        report["stages"][stage] = (
            "blocked"
            if report["error_code"] in {"process_busy", "resource_busy", "test_precondition"}
            else "failed"
        )
        if app and device_id and initial:
            try:
                # A failed transport is not reused and no test command is replayed.
                session = app.sessions.sessions.get(device_id)
                if session and session.state in {"degraded", "error"}:
                    app.sessions.disconnect(device_id)
                    app.connect(device_id, configure_acquisition=False).result(timeout=60)
                observed = snapshot()
                report["recovery_observed_snapshot"] = observed
                if pending:
                    ch, name, original = pending
                    now = basic_value(basic(observed[f"C{ch}:BSWV?"].encode(), ch), name)
                    if not math.isclose(now, original, rel_tol=1e-9, abs_tol=1e-9) and not restore_attempted:
                        report["recovery_apply"] = write(ch, name, original)
                report["final_snapshot"] = snapshot()
                differences = compare_snapshots(initial, report["final_snapshot"])
                report["restoration_differences"] = differences
                report["stages"]["restoration"] = (
                    "failed" if differences else "passed" if pending or report["operations"] else "not_tested"
                )
            except Exception as recovery:
                report["restoration_warning"] = str(recovery)
                report["stages"]["restoration"] = "failed"
                report["final_state"] = "Unknown; restoration could not be verified"
    finally:
        if app:
            try:
                app.close()
                report["session_released"] = True
            except Exception as close:
                report["close_error"] = str(close)
        report["finished_at"] = utc_now()
        (folder / "results.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        rows = "\n".join(f"| {key} | {value} |" for key, value in report["stages"].items())
        (folder / "record.md").write_text(
            f"# SDG6032X-E LAN 基本控制验收\n\n{report['started_at']} — {report['finished_at']}\n\n| 项目 | 结果 |\n| --- | --- |\n{rows}\n\n双通道正弦参数逐项写入、读回和恢复，保持输出关闭；每步核验另一通道、输出、模式及全局设置。未执行输出开启、切换波形、负载、调制、扫频、突发、波形上传或物理测量。\n\n错误：{report.get('error', '无')}\n\n恢复警告：{report.get('restoration_warning', '无')}\n\n[原始结果](results.json) · [通信](communication.jsonl) · [测试计划](plan.json)\n",
            encoding="utf-8",
        )
        publish_report(folder)
        refresh_after_batch(folder)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--write", action="store_true", help="Authorize the output-OFF write/read/restore plan"
    )
    args = parser.parse_args()
    configs = load_config(args.config)
    if not args.write or len(configs) != 1:
        parser.error("Require --write and exactly one configured asset")
    config = configs[0]
    folder = (
        application_home()
        / "hardware-tests"
        / asset_folder_name("SDG6032X-E", config.expected_serial)
        / (datetime.now().strftime("%Y-%m-%d-%H%M%S-%f") + "-lan-control")
    )
    report = verify(config, folder)
    print(folder)
    print(json.dumps(report["stages"], ensure_ascii=False))
    if report.get("error"):
        print(report["error"])
    return int(bool(report.get("error") or report.get("close_error")))


if __name__ == "__main__":
    raise SystemExit(main())
