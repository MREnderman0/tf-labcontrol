"""Explicit unloaded E00 standard-wave delivery/restoration test, one selected port."""

from __future__ import annotations

import argparse
import json
import math
import platform
import time
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceCommand, DeviceError, jsonable, utc_now
from lab_control.instruments.common.hardware_paths import verify_runtime_paths
from lab_control.instruments.registry import create_adapter
from lab_control.storage.hardware_records import atomic_json, digest
from lab_control.storage.repository import application_home

from ..adapter import CoreMorrowXmtAdapter
from .readonly import _enumerate
from .records import publish_report
from .validate_control import PORT_PATTERN, TraceTransport, _read_value, _safe_asset_name


def run(
    resource: str, asset_name: str, expected_usb_serial: str, *, unloaded: bool, dwell: float = 1.2
) -> Path:
    resource = resource.upper()
    if not PORT_PATTERN.fullmatch(resource) or not expected_usb_serial or not unloaded:
        raise ValueError("Specify a single COM port, the selected USB bridge serial and --unloaded")
    if not 0.1 <= dwell <= 3:
        raise ValueError("dwell must be between 0.1 and 3 seconds")
    root = application_home()
    folder = (
        root
        / "hardware-tests"
        / _safe_asset_name(asset_name)
        / (datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f") + "-waveform-" + uuid4().hex[:8])
    )
    folder.mkdir(parents=True, exist_ok=False)
    transcript = folder / "communication.jsonl"
    transcript.touch()
    stages = {
        k: {"status": "not_tested", "reason": "Not reached"}
        for k in (
            "enumeration",
            "identity",
            "compatibility_connection",
            "parameter_read",
            "parameter_write",
            "waveform_command_delivery",
            "channel_isolation",
            "restoration",
            "reconnection",
            "physical_output",
        )
    }
    report: dict[str, Any] = {
        "adapter_id": "coremorrow.xmt",
        "model": "E00.C3-13",
        "resource": resource,
        "interface": "serial",
        "identity": {
            "manufacturer": "CoreMorrow",
            "model": "E00.C3-13",
            "serial": "",
            "firmware": "",
            "confidence": "operator_observed",
        },
        "expected_serial": "",
        "usb_bridge_serial": expected_usb_serial,
        "started_at": utc_now(),
        "scope": "Three unloaded channels; four standard waves at 1/2 Hz, 0.2/0.4 Vpp, 0.1/0.3 V bias; all targets <=0.5 V. No physical measurement.",
        "environment": {
            "python": platform.python_version(),
            "runtime_home": str(root),
            "load": "all three outputs unloaded (operator confirmed)",
        },
        "source_sha256": {
            p.name: digest(p)
            for p in (
                Path(__file__),
                Path(__file__).parents[1] / "adapter.py",
                Path(__file__).parents[1] / "waveform.py",
                Path(__file__).parents[1] / "protocol.py",
            )
        },
        "stages": stages,
        "operations": [],
        "final_state": "No hardware I/O attempted",
    }
    counters = {"ports_opened": 0, "ports_closed": 0, "commands_sent": 0}
    runtime: ApplicationRuntime | None = None
    device_id = f"waveform-test:{asset_name}"
    stage = "enumeration"
    modified = False
    recovered = False
    initial: dict[str, float] = {}

    def save():
        report["counters"] = dict(counters)
        atomic_json(folder / "results.json", report)

    def apply(operation, channel=1, parameters=None):
        nonlocal modified
        assert runtime is not None
        entry = {
            "operation": operation,
            "channel": channel,
            "parameters": parameters or {},
            "started_at": utc_now(),
        }
        report["operations"].append(entry)
        save()
        modified = True
        result = runtime.sessions.submit(
            device_id, "apply", DeviceCommand(operation, parameters or {}, channel, True)
        ).result(30)
        entry["result"] = jsonable(result)
        entry["finished_at"] = utc_now()
        save()
        if result.outcome != "success":
            raise DeviceError(result.error, "write_uncertain")
        return result

    save()
    try:
        enumeration = _enumerate(resource)
        report["enumeration"] = enumeration
        atomic_json(folder / "enumeration.json", enumeration)
        matches = enumeration.get("serial_ports", [])
        if len(matches) != 1 or expected_usb_serial.casefold() not in matches[0]["hwid"].casefold():
            raise DeviceError(
                "Selected USB bridge identity is absent or differs; no port opened", "identity_conflict"
            )
        stages[stage] = {
            "status": "passed",
            "reason": "Selected COM port and USB bridge serial enumerated; bridge serial is not instrument serial",
        }
        stage = "compatibility_connection"
        runtime = ApplicationRuntime(hardware=True)
        report["environment"]["resolved_paths"] = verify_runtime_paths(runtime.repository.root, root)
        config = ConnectionConfig(resource, "coremorrow.xmt", "serial", baud_rate=9600, timeout=3)

        def factory(selected):
            if selected.resource != resource or selected.adapter != "coremorrow.xmt":
                raise DeviceError("Only the selected E00 is authorized", "identity_conflict")
            adapter = cast(CoreMorrowXmtAdapter, create_adapter(selected, runtime.visa))
            counters["ports_opened"] += 1
            adapter.transport = TraceTransport(adapter.transport, transcript, counters)  # type: ignore[assignment]
            return adapter

        runtime.sessions.factory = factory
        identity = runtime.sessions.connect(device_id, config, configure_acquisition=False).result(30)
        report["identity"] = jsonable(identity)
        stages["identity"] = {
            "status": "blocked",
            "reason": "XMT configuration identifies protocol and three channels, but instrument serial/firmware/exact model are not reported; operator nameplate E00.C3-13 retained",
        }
        stages[stage] = {
            "status": "passed",
            "reason": "Project session connected under formal process/resource locks",
        }
        stage = "parameter_read"
        status = runtime.sessions.submit(device_id, "status").result(15)
        if status.values["power_config"]["channel_count"] != 3:
            raise DeviceError("Expected three channels", "identity_conflict")
        initial = {str(ch): _read_value(runtime, device_id, ch) for ch in (1, 2, 3)}
        report["initial_snapshot"] = {
            "status": jsonable(status),
            "voltage": initial,
            "mode": "unknown; voltage samples do not establish mode",
        }
        atomic_json(folder / "initial-state.json", report["initial_snapshot"])
        if any(not math.isfinite(v) or not -0.01 <= v <= 120.01 for v in initial.values()):
            raise DeviceError(
                "Initial voltage outside the supported restoration range; no writes", "test_gate"
            )
        stages[stage] = {
            "status": "passed",
            "reason": "Saved configuration and three voltage readbacks before any write; operating mode remains unknown",
        }
        stage = "waveform_command_delivery"
        # An explicit test action, never connection initialization or an automatic replay.
        apply("normal_mode")
        normalized = {str(ch): _read_value(runtime, device_id, ch) for ch in (1, 2, 3)}
        report["pre_wave_dc"] = normalized
        if any(abs(normalized[k] - initial[k]) > 0.01 for k in initial):
            raise DeviceError(
                "Voltage changed after stopping; original mode unknown, abort wave test", "test_gate"
            )
        for channel in (1, 2, 3):
            for shape in ("SIN", "SQUARE", "TRIANGLE", "SAWTOOTH"):
                apply(
                    "standard_waveform",
                    channel,
                    {"waveform": shape, "frequency": 1.0, "amplitude": 0.2, "offset": 0.1},
                )
                time.sleep(dwell)
                apply("waveform_stop", channel)
                apply("normal_mode")
            for parameters in (
                {"waveform": "SIN", "frequency": 2.0, "amplitude": 0.2, "offset": 0.1},
                {"waveform": "SIN", "frequency": 2.0, "amplitude": 0.4, "offset": 0.3},
            ):
                apply("standard_waveform", channel, parameters)
                time.sleep(dwell)
                apply("normal_mode")
        for channel in (1, 2, 3):
            apply(
                "standard_waveform",
                channel,
                {"waveform": "TRIANGLE", "frequency": 1.0, "amplitude": 0.2, "offset": 0.1},
            )
        time.sleep(dwell)
        apply("waveform_stop", 2)
        snapshot = runtime.sessions.capability_snapshot(device_id)
        assert snapshot is not None
        report["after_middle_stop"] = snapshot.details
        apply("normal_mode")
        stages[stage] = {
            "status": "passed",
            "reason": "Official-encoded frames delivered once per action on three channels; commands have no acknowledgment and no parameter/status readback",
        }
        stages["parameter_write"] = {
            "status": "not_tested",
            "reason": "Waveform parameters cannot be independently read back; transport delivery is recorded separately",
        }
        stages["channel_isolation"] = {
            "status": "not_tested",
            "reason": "Stop frame addresses CH2 and command records retain CH1/3; physical isolation requires external measurement",
        }
        stages["physical_output"] = {
            "status": "not_tested",
            "reason": "No external instrument was connected to measure waveform shape, frequency, amplitude or cessation",
        }
        stage = "restoration"
        final = {str(ch): _read_value(runtime, device_id, ch) for ch in (1, 2, 3)}
        snapshot = runtime.sessions.capability_snapshot(device_id)
        assert snapshot is not None
        report["final_snapshot"] = {"voltage": final, "control_state": snapshot.details}
        atomic_json(folder / "final-state.json", report["final_snapshot"])
        if any(abs(final[k] - initial[k]) > 0.01 for k in initial):
            raise DeviceError("Final DC readings differ from initial snapshot", "restoration_failed")
        recovered = True
        stages[stage] = {
            "status": "passed",
            "reason": "Saved DC values restored and independent voltage readback agrees within 0.01 V; physical waveform cessation remains unmeasured",
        }
        stage = "reconnection"
        runtime.sessions.disconnect(device_id)
        runtime.sessions.connect(device_id, config, configure_acquisition=False).result(30)
        report["reconnected_status"] = jsonable(runtime.sessions.submit(device_id, "status").result(15))
        report["reconnected_voltages"] = {str(ch): _read_value(runtime, device_id, ch) for ch in (1, 2, 3)}
        stages[stage] = {
            "status": "passed",
            "reason": "New session reads identity/configuration/voltages; mode remains unknown and no output is replayed",
        }
        report["final_state"] = (
            "Waveform frame delivery completed; DC restored in software; no independent waveform/physical output verification"
        )
    except Exception as exc:
        report["error"] = str(exc)
        code = getattr(exc, "code", type(exc).__name__)
        stages[stage] = {
            "status": "blocked"
            if code in {"resource_busy", "timeout", "driver_missing", "identity_conflict", "test_gate"}
            else "failed",
            "reason": str(exc),
        }
        report["final_state"] = (
            "Test stopped; no automatic command replay. Restoration and physical output are unconfirmed unless separately recorded."
        )
    finally:
        if runtime is not None:
            try:
                runtime.close()
                if modified and not recovered:
                    report["shutdown_note"] = (
                        "Normal close ran the adapter release hook when communication remained usable; no independent final readback captured, restoration not claimed"
                    )
            except Exception as exc:
                report["shutdown_error"] = str(exc)
        report["finished_at"] = utc_now()
        report["final_outputs"] = {
            "write_attempted": modified,
            "dc_restoration_verified": recovered,
            "physical_output_verified": False,
        }
        report["evidence_sha256"] = {"communication.jsonl": digest(transcript)}
        save()
        rows = "\n".join(f"| {k} | {v['status']} | {v['reason']} |" for k, v in stages.items())
        (folder / "record.md").write_text(
            "# E00 标准波形控制与恢复测试\n\n"
            + report["final_state"]
            + "\n\n| 项目 | 状态 | 依据 |\n| --- | --- | --- |\n"
            + rows
            + "\n\n波形命令没有状态/参数读回；软件发送成功不是物理输出验收。原始通信见 communication.jsonl。\n",
            encoding="utf-8",
        )
        publish_report(folder)
    return folder


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource", required=True)
    parser.add_argument("--asset-name", required=True)
    parser.add_argument("--expected-usb-serial", required=True)
    parser.add_argument("--unloaded", action="store_true")
    args = parser.parse_args()
    folder = run(args.resource, args.asset_name, args.expected_usb_serial, unloaded=args.unloaded)
    data = json.loads((folder / "results.json").read_text(encoding="utf-8"))
    print(
        json.dumps(
            {"folder": str(folder), "stages": data["stages"], "final_state": data["final_state"]},
            ensure_ascii=False,
        )
    )
    return int(bool(data.get("error")))


if __name__ == "__main__":
    raise SystemExit(main())
