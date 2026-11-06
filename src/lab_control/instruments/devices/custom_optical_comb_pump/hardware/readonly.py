"""Opt-in single-port hardware acceptance through the manual session path."""

import json
import re
from pathlib import Path
from typing import Any

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceError, jsonable, utc_now

from ..adapter import (
    ADAPTER_ID,
    MODEL,
    READ_SETTINGS,
    TELEMETRY,
    FixedTransport,
    OpticalCombPumpAdapter,
    frame,
)
from .records import summarize


class ReadOnlyTrace:
    def __init__(self, wire: FixedTransport, transcript: Path) -> None:
        self.wire, self.transcript = wire, transcript

    def _append(self, **data: Any) -> None:
        with self.transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": utc_now(), **data}, ensure_ascii=False) + "\n")

    def query_fixed(self, payload: bytes, count: int) -> bytes:
        if payload not in {frame(READ_SETTINGS), frame(TELEMETRY["current"][0])}:
            raise DeviceError("只读测试仅允许 CH 1 设置和实测电流查询", "unsupported")
        self._append(direction="tx", frame=payload.hex(" "))
        try:
            reply = self.wire.query_fixed(payload, count)
        except Exception as exc:
            self._append(direction="error", error=str(exc))
            raise
        self._append(direction="rx", frame=reply.hex(" "))
        return reply

    def close(self) -> None:
        self.wire.close()


def verify(
    config: ConnectionConfig,
    folder: Path,
    *,
    duration: float,
    root: Path | None,
    input_condition: str,
) -> dict[str, Any]:
    from lab_control.application.runtime import ApplicationRuntime
    from lab_control.discovery.providers import SerialDiscoveryProvider
    from lab_control.instruments.registry import create_adapter
    from lab_control.storage.hardware_documents import refresh_after_batch
    from lab_control.storage.hardware_records import atomic_json, publish_report
    from lab_control.storage.repository import application_home

    hardware_root = (root or application_home()).resolve() / "hardware-tests"
    folder = folder.resolve()
    if folder.parent.parent != hardware_root:
        raise ValueError("批次目录须为运行目录下的 hardware-tests/<人工资产号>/<批次>")
    folder.mkdir(parents=True, exist_ok=False)
    transcript = folder / "communication.jsonl"
    transcript.touch()
    report: dict[str, Any] = {
        "adapter_id": ADAPTER_ID,
        "model": MODEL,
        "resource": config.resource,
        "interface": "VISA ASRL / RS232",
        "started_at": utc_now(),
        "input_condition": input_condition,
        "scope": "单个操作员选定的串口，只读 CH 1 设置与实测电流",
        "stages": {
            name: {"state": "not_tested"}
            for name in (
                "enumeration",
                "identity",
                "compatibility_connection",
                "parameter_read",
                "current_read",
                "parameter_write",
                "restoration",
                "reconnection",
                "physical_output",
                "disconnect",
            )
        },
        "checks_passed": False,
        "final_state": "输出状态未知，未执行设置写入",
    }
    stages = report["stages"]
    stage = "enumeration"
    try:
        port = re.fullmatch(r"(?:COM|ASRL)([1-9][0-9]*)(?:::INSTR)?", config.resource, re.I)
        if config.adapter != ADAPTER_ID or config.transport != "visa" or port is None:
            raise DeviceError("请选择一个明确的 Windows COM 口并使用 VISA", "invalid_parameter")
        selected = f"COM{port[1]}"
        evidence = [
            jsonable(item)
            for item in SerialDiscoveryProvider().enumerate()
            if item.config.resource.upper() == selected
        ]
        atomic_json(folder / "enumeration.json", evidence)
        if not evidence:
            raise DeviceError("所选 COM 口未枚举", "driver_missing")
        stages[stage] = {"state": "passed"}
        stage = "compatibility_connection"
        with ApplicationRuntime(root=root) as runtime:

            def traced(connection: ConnectionConfig):
                adapter = create_adapter(connection, runtime.visa)
                assert isinstance(adapter, OpticalCombPumpAdapter)
                adapter.transport = ReadOnlyTrace(adapter.transport, transcript)
                return adapter

            runtime.sessions.factory = traced
            device_id, connected = runtime.connect_manual_serial(ADAPTER_ID, selected)
            identity = connected.result(timeout=duration + 10)
            report["identity"] = jsonable(identity)
            stages["identity"] = {"state": "blocked", "reason": "人工选口，协议无身份字段"}
            stages[stage] = {"state": "passed", "reason": "正式会话收到 CH 1 完整设置回复"}
            stage = "parameter_read"
            status = runtime.sessions.submit(device_id, "status").result(timeout=duration + 10)
            report["settings"] = jsonable(status)
            report["final_state"] = f"最后一次设备读回输出值为 {status.values['output']}，未执行设置写入"
            stages[stage] = {"state": "passed"}
            stage = "current_read"
            sample = runtime.sessions.submit(device_id, "acquire", AcquisitionRequest()).result(
                timeout=duration + 10
            )
            report["current"] = jsonable(sample)
            stages[stage] = {"state": "passed", "reason": "仅核对协议读回"}
            stage = "disconnect"
        stages[stage] = {"state": "passed"}
        report["checks_passed"] = True
    except Exception as exc:
        code = getattr(exc, "code", "")
        stages[stage] = {
            "state": "blocked"
            if code in {"driver_missing", "resource_busy", "invalid_parameter"}
            else "failed",
            "reason": str(exc),
        }
    finally:
        report["finished_at"] = utc_now()
        atomic_json(folder / "results.json", report)
        publish_report(folder, summarize)
        refresh_after_batch(folder)
    return report
