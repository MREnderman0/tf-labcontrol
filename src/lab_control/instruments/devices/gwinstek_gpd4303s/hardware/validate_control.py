"""Explicit, single-asset verification. Never launched on import or by the GUI."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pyvisa

from lab_control.application.config import ConnectionConfig, load_config
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceCommand, DeviceError, jsonable, utc_now
from lab_control.instruments.registry import create_adapter
from lab_control.storage.repository import application_home
from lab_control.transport.base import MockTransport, Transport

from ..adapter import Gpd4303sAdapter, gpd4303s
from ..protocol import PacedTransport
from .records import publish_report

SETTINGS = ("voltage", "current")
READINGS = (*SETTINGS, "measured_voltage", "measured_current")
LOW_VOLTAGES = {1: 1.0, 2: 1.5, 3: 2.0, 4: 2.5}


def validate_restoration_target(target: dict, config: ConnectionConfig) -> None:
    """Do not start a test whose recorded recovery target cannot be legally written."""
    adapter = Gpd4303sAdapter(MockTransport([]), gpd4303s(), config)
    for ch in range(1, 5):
        for op in SETTINGS:
            adapter.validate_command(DeviceCommand(op, {"value": target[f"{ch}.{op}"]}, ch))
    if target["3.voltage"] > 5 and target["3.current"] > 1:
        raise DeviceError("CH3 restoration combination is invalid", "invalid_parameter")
    adapter.validate_command(
        DeviceCommand("tracking", {"value": target["status"]["tracking"]}, confirmed=True)
    )


class RecordingTransport:
    def __init__(self, inner: Transport, log: Path, control: bool):
        self.inner, self.log, self.control = inner, log, control
        self.resource = inner.resource

    def record(self, direction: str, data: bytes | str) -> None:
        raw = data.encode("ascii") if isinstance(data, str) else data
        with self.log.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "time": utc_now(),
                        "direction": direction,
                        "hex": raw.hex(),
                        "text": raw.decode("ascii", "backslashreplace"),
                    }
                )
                + "\n"
            )

    def check(self, payload: bytes | str) -> None:
        readonly = isinstance(payload, str) and re.fullmatch(
            r"\*IDN\?|STATUS\?|(?:VSET|ISET|VOUT|IOUT)[1-4]\?", payload
        )
        setting = (
            self.control
            and isinstance(payload, str)
            and re.fullmatch(r"OUT[01]|TRACK[012]|REMOTE|LOCAL|(?:VSET|ISET)[1-4]:\d+\.\d{3}", payload)
        )
        if not (readonly or setting or payload == "REMOTE"):
            raise DeviceError("Command outside the approved hardware plan", "unsupported")

    def open(self) -> None:
        raise RuntimeError("Session owns open")

    def close(self) -> None:
        self.inner.close()

    def write(self, payload: bytes | str) -> None:
        self.check(payload)
        self.record("tx", payload)
        self.inner.write(payload)

    def read(self) -> bytes:
        try:
            data = self.inner.read()
            self.record("rx", data)
            return data
        except Exception as exc:
            self.record("error", str(exc).encode("utf-8"))
            raise

    def query(self, payload: bytes | str) -> bytes:
        self.write(payload)
        return self.read()

    def query_fixed(self, payload: bytes | str, count: int) -> bytes:
        self.check(payload)
        self.record("tx", payload)
        try:
            fixed = getattr(self.inner, "query_fixed", None)
            data = fixed(payload, count) if fixed else self.inner.query(payload)
            self.record("rx_fixed", data)
            return data
        except Exception as exc:
            self.record("error", str(exc).encode("utf-8"))
            raise

    def read_pending(self) -> bytes:
        pending = getattr(self.inner, "read_pending", None)
        raw = pending() if pending else b""
        if raw:
            self.record("rx_suffix", raw)
        return raw


def differences(actual: dict, expected: dict) -> dict[str, Any]:
    result = {}
    for ch in range(1, 5):
        for op in SETTINGS:
            key = f"{ch}.{op}"
            if not math.isclose(actual[key], expected[key], rel_tol=0, abs_tol=0.001000001):
                result[key] = {"expected": expected[key], "actual": actual[key]}
    if actual["status"]["tracking"] != expected["status"]["tracking"]:
        result["tracking"] = {
            "expected": expected["status"]["tracking"],
            "actual": actual["status"]["tracking"],
        }
    return result


class TestSession:
    def __init__(self, app: ApplicationRuntime, device_id: str, report: dict, folder: Path):
        self.app, self.device_id, self.report, self.folder = app, device_id, report, folder

    def read(self, operation: str, channel: int = 1) -> Any:
        result = self.app.sessions.submit(self.device_id, "read_settings", ((operation,), channel)).result(30)
        return result[0].value

    def snapshot(self) -> dict[str, Any]:
        values = {f"{ch}.{op}": self.read(op, ch) for ch in range(1, 5) for op in READINGS}
        values["status"] = self.read("status_register")
        return values

    def apply(self, operation: str, value: Any = None, channel: int = 1) -> None:
        cmd = DeviceCommand(operation, {} if value is None else {"value": value}, channel, True)
        entry = {"started_at": utc_now(), "request": jsonable(cmd)}
        self.report["operations"].append(entry)
        try:
            result = self.app.sessions.submit(self.device_id, "apply", cmd).result(30)
            entry["result"] = jsonable(result)
            if result.outcome != "success":
                raise DeviceError(result.error or result.outcome, "readback_mismatch")
        except Exception as exc:
            entry["error"] = str(exc)
            raise
        finally:
            entry["finished_at"] = utc_now()
            self.save()

    def save(self) -> None:
        (self.folder / "results.json").write_text(
            json.dumps(self.report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def require_off(self) -> None:
        if self.read("all_outputs") is not False:
            raise DeviceError("Master output is not confirmed OFF", "output_not_off")

    def restore(self, original: dict) -> None:
        # Reopen only after a failed session; this never replays its failed command.
        session = self.app.sessions.sessions[self.device_id]
        if session.state == "degraded":
            self.app.sessions.disconnect(self.device_id)
            self.app.connect(self.device_id).result(30)
        current = self.snapshot()
        if current["status"]["output_enabled"]:
            self.apply("all_outputs", False)
        self.require_off()
        if self.read("tracking") != "0":
            self.apply("tracking", "0")
        for ch in range(1, 5):
            if ch == 3 and self.read("current", 3) > 1 and original["3.voltage"] > 5:
                self.apply("current", original["3.current"], 3)
            for op in SETTINGS:
                value = original[f"{ch}.{op}"]
                if not math.isclose(self.read(op, ch), value, rel_tol=0, abs_tol=0.001000001):
                    self.apply(op, value, ch)
        if self.read("tracking") != original["status"]["tracking"]:
            self.apply("tracking", original["status"]["tracking"])
        final = self.snapshot()
        self.report["final_snapshot"] = final
        self.report["restoration_differences"] = differences(final, original)
        self.report["final_output_state"] = final["status"]["output_enabled"]
        if self.report["restoration_differences"] or final["status"]["output_enabled"]:
            raise DeviceError("Restoration could not be verified", "restore_failed")

    def control_tests(self) -> None:
        self.report["active_stage"] = "parameter_control"
        self.apply("remote")
        self.apply("all_outputs", False)
        self.require_off()
        self.apply("tracking", "0")
        for ch, voltage in LOW_VOLTAGES.items():
            for op, value in (("current", 0.1), ("voltage", voltage)):
                before = self.snapshot()
                self.apply(op, value, ch)
                after = self.snapshot()
                expected = {**before, f"{ch}.{op}": value}
                changes = differences(after, expected)
                self.report["operations"][-1]["isolation_differences"] = changes
                if changes:
                    raise DeviceError("Unexpected channel setting change", "readback_mismatch")
        self.report["stages"]["parameter_control"] = "passed"
        self.report["active_stage"] = "ch3_ranges"
        for voltage in (5.0, 5.001, 10.0, LOW_VOLTAGES[3]):
            self.require_off()
            self.apply("voltage", voltage, 3)
            self.report["range_snapshots"].append(
                jsonable(self.app.sessions.capability_snapshot(self.device_id))
            )
        self.report["stages"]["ch3_ranges"] = "passed"
        self.report["active_stage"] = "tracking"
        for mode in ("1", "2", "0"):
            self.require_off()
            self.apply("tracking", mode)
            self.report["mode_snapshots"].append(self.snapshot())
        self.report["stages"]["tracking"] = "passed"
        self.report["active_stage"] = "unloaded_output"
        for ch, voltage in LOW_VOLTAGES.items():
            self.apply("current", 0.1, ch)
            self.apply("voltage", voltage, ch)
        self.require_off()
        before_on = self.snapshot()
        if before_on["status"]["tracking"] != "0":
            raise DeviceError("Output test requires independent mode", "invalid_parameter")
        for ch, voltage in LOW_VOLTAGES.items():
            if (
                abs(before_on[f"{ch}.voltage"] - voltage) > 0.001000001
                or abs(before_on[f"{ch}.current"] - 0.1) > 0.001000001
            ):
                raise DeviceError("Low-voltage/current-limit preflight failed", "readback_mismatch")
        self.report["pre_output_snapshot"] = before_on
        self.apply("all_outputs", True)
        time.sleep(1)
        measured = self.snapshot()
        self.report["output_on_snapshot"] = measured
        self.apply("all_outputs", False)
        self.require_off()
        passed = measured["status"]["output_enabled"] and all(
            abs(measured[f"{ch}.measured_voltage"] - voltage) <= 0.05
            and abs(measured[f"{ch}.measured_current"]) <= 0.02
            for ch, voltage in LOW_VOLTAGES.items()
        )
        if not passed:
            raise DeviceError("Unloaded instrument readings exceed plan tolerance", "readback_mismatch")
        self.report["stages"]["unloaded_output"] = "passed"


def verify(
    config: ConnectionConfig, folder: Path, *, control: bool = False, final_ch4_current: float | None = None
) -> dict[str, Any]:
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "communication.jsonl").touch()
    report: dict[str, Any] = {
        "adapter_id": config.adapter,
        "config": asdict(config),
        "resource": config.resource,
        "expected_serial": config.expected_serial,
        "started_at": utc_now(),
        "scope": "Four outputs unloaded; low voltage, 0.100 A limit; output OFF during mode/range tests; restore settings and leave output OFF/local"
        if control
        else "Selected USB asset: REMOTE connection setup, read-only electrical settings; no parameter/output writes",
        "environment": {
            "python": platform.python_version(),
            "pyvisa": pyvisa.__version__,
            "platform": platform.platform(),
        },
        "source_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (
                Path(__file__),
                Path(__file__).parents[1] / "adapter.py",
                Path(__file__).parents[1] / "protocol.py",
            )
        },
        "stages": {
            key: "not_tested"
            for key in (
                "identity",
                "connection",
                "parameter_read",
                "parameter_control",
                "ch3_ranges",
                "tracking",
                "unloaded_output",
                "restoration",
                "reconnection",
                "physical_output",
                "panel_local",
            )
        },
        "operations": [],
        "range_snapshots": [],
        "mode_snapshots": [],
        "final_state": "Unknown until a final snapshot has been obtained",
    }
    app = None
    test = None
    original = None
    restoration_target = None
    mutation_started = False
    stage = "connection"
    try:
        if config.adapter != "gwinstek.gpd4303s" or not config.expected_serial or config.transport != "visa":
            raise ValueError("Use one explicit GPD-4303S VISA config with its full expected serial")
        lock = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "TF-LabControl/hardware.lock"
        if (
            lock.resolve() != lock
            or application_home().resolve() != application_home()
            or folder.resolve() != folder
        ):
            raise DeviceError(
                "Run in the native application's physical data/lock environment", "path_redirected"
            )
        app = ApplicationRuntime((config,))

        def factory(selected):
            if selected != config:
                raise ValueError("Hardware verification cannot select a different asset")
            adapter = create_adapter(selected, app.visa)
            assert isinstance(adapter, Gpd4303sAdapter)
            assert isinstance(adapter.transport, PacedTransport)
            adapter.transport.inner = RecordingTransport(
                adapter.transport.inner, folder / "communication.jsonl", control
            )
            return adapter

        app.sessions.factory = factory
        device_id = app.add(config)
        report["inventory_id"] = device_id
        identity = app.connect(device_id).result(30)
        report["identity"] = jsonable(identity)
        report["environment"]["visa"] = str(app.visa.manager.visalib)
        report["stages"].update(identity="passed", connection="passed")
        test = TestSession(app, device_id, report, folder)
        stage = "parameter_read"
        original = test.snapshot()
        if original["status"]["tracking"] is None:
            raise DeviceError("Cannot back up an unknown tracking mode", "parse_error")
        report["initial_snapshot"] = original
        backup = json.dumps(original, ensure_ascii=False, indent=2).encode("utf-8")
        (folder / "initial_snapshot.json").write_bytes(backup)
        report["initial_snapshot_sha256"] = hashlib.sha256(backup).hexdigest()
        report["stages"][stage] = "passed"
        test.save()
        if control:
            restoration_target = dict(original)
            if final_ch4_current is not None:
                restoration_target["4.current"] = final_ch4_current
                report["approved_restoration_change"] = {
                    "4.current": {"original": original["4.current"], "target": final_ch4_current},
                    "reason": "Explicit --final-ch4-current; original retained in immutable backup",
                }
            report["restoration_target"] = restoration_target
            validate_restoration_target(restoration_target, config)
            stage = "control_sequence"
            mutation_started = True
            test.control_tests()
        else:
            for _ in range(5):
                test.read("status_register")
                test.read("voltage", 1)
            report["final_snapshot"] = test.snapshot()
            if (
                differences(report["final_snapshot"], original)
                or report["final_snapshot"]["status"] != original["status"]
            ):
                raise DeviceError("Settings changed during read-only verification", "readback_mismatch")
            report["final_output_state"] = report["final_snapshot"]["status"]["output_enabled"]
            report["final_state"] = (
                "Read-only snapshots agree; no electrical parameter/output writes; REMOTE used for connection"
            )
    except Exception as exc:
        report["error"] = str(exc)
        report["error_code"] = getattr(exc, "code", type(exc).__name__)
        stage = report.get("active_stage", stage)
        report["stages"][stage] = (
            "blocked"
            if report["error_code"]
            in {
                "process_busy",
                "path_redirected",
                "resource_busy",
                "timeout",
                "disconnected",
                "connection_failed",
            }
            else "failed"
        )
    finally:
        if mutation_started and test and original and restoration_target and app:
            try:
                test.restore(restoration_target)
                report["original_differences"] = differences(report["final_snapshot"], original)
                report["stages"]["restoration"] = "passed"
                app.sessions.disconnect(test.device_id)
                identity = app.connect(test.device_id).result(30)
                final = test.snapshot()
                report["reconnected_identity"] = jsonable(identity)
                report["final_snapshot"] = final
                if differences(final, restoration_target) or final["status"]["output_enabled"]:
                    raise DeviceError("Reconnection state differs from restored state", "readback_mismatch")
                report["stages"]["reconnection"] = "passed"
                test.apply("local")
                report["local_command_sent"] = True
                report["final_state"] = (
                    "Approved restoration target verified after reconnect; output OFF; LOCAL sent, panel confirmation pending"
                )
            except Exception as exc:
                report["restore_error"] = str(exc)
                key = "restoration" if report["stages"]["restoration"] != "passed" else "reconnection"
                report["stages"][key] = "failed"
                report["final_state"] = (
                    "See last timestamped snapshot; final state not fully confirmed: " + str(exc)
                )
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
        rows = "\n".join(f"| {key} | {value} |" for key, value in report["stages"].items())
        (folder / "record.md").write_text(
            "# GPD-4303S USB 验证\n\n"
            + report["scope"]
            + "\n\n"
            + f"时间：{report['started_at']} 至 {report['finished_at']}\n\n"
            + "| 项目 | 结果 |\n| --- | --- |\n"
            + rows
            + "\n\n"
            + "最终状态："
            + report["final_state"]
            + "\n\n错误："
            + report.get("error", "无")
            + "\n\n仪器内部读回不代表外部测量精度或带载验收。未执行存储、复位、波特率或固件修改。\n\n"
            + "[原始结果](results.json) · [摘要](summary.json) · [通信](communication.jsonl)\n",
            encoding="utf-8",
        )
        with (folder.parents[1] / "index.md").open("a", encoding="utf-8") as stream:
            stream.write(
                f"\n- {report['finished_at']}：GPD-4303S USB {'空载控制' if control else '只读'}；"
                f"[记录]({folder.parent.name}/{folder.name}/record.md)；"
                f"恢复 {report['stages']['restoration']}，物理输出未验收。\n"
            )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--control-unloaded", action="store_true", help="Explicit approved empty-terminal output test"
    )
    parser.add_argument("--asset-name", default="")
    parser.add_argument(
        "--final-ch4-current",
        type=float,
        default=None,
        help="Explicit approved CH4 final current when the original exceeds rated limits",
    )
    args = parser.parse_args()
    configs = load_config(args.config)
    if len(configs) != 1:
        parser.error("One selected asset required")
    config = configs[0]
    asset = args.asset_name or "GPD-4303S-" + re.sub(r"[^A-Za-z0-9_.-]", "-", config.expected_serial)
    if Path(asset).name != asset or asset in {".", ".."}:
        parser.error("Invalid asset name")
    folder = (
        application_home()
        / "hardware-tests"
        / asset
        / (
            datetime.now().strftime("%Y-%m-%d-%H%M%S-%f")
            + ("-usb-unloaded-control" if args.control_unloaded else "-usb-readonly")
        )
    )
    report = verify(config, folder, control=args.control_unloaded, final_ch4_current=args.final_ch4_current)
    print(
        json.dumps(
            {
                "folder": str(folder),
                "stages": report["stages"],
                "error": report.get("error", ""),
                "restore_error": report.get("restore_error", ""),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return int(bool(report.get("error") or report.get("restore_error")))


if __name__ == "__main__":
    raise SystemExit(main())
