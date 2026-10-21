"""Explicit read-only K+K FXE compatibility verification entry point.

This module never scans addresses or changes device settings.  The caller must
pass one explicitly selected endpoint and the resulting batch is written to
the runtime hardware-tests directory.
"""

from __future__ import annotations

import argparse
import json
import platform
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest, DeviceError, jsonable, utc_now
from lab_control.instruments.registry import create_adapter
from lab_control.storage.repository import application_home


def verify(config: ConnectionConfig, folder: Path) -> dict[str, Any]:
    """Run one explicit endpoint through the project compatibility layer."""
    folder.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "schema_version": 1,
        "adapter_id": config.adapter,
        "model": "K+K FXE (完整变体待核对)",
        "expected_serial": config.expected_serial,
        "resource": config.resource,
        "interface": "K+K direct Ethernet via switch",
        "started_at": utc_now(),
        "scope": "项目兼容层只读验证；未扫描地址、未写设备设置、未改电脑网卡",
        "stages": {
            key: {"state": "not_tested"}
            for key in (
                "enumeration",
                "identity",
                "official_protocol_connection",
                "compatibility_connection",
                "parameter_read",
                "frequency_read",
                "parameter_write",
                "restoration",
                "reconnection",
                "physical_output",
            )
        },
        "setting_write_api_calls": 0,
        "environment": {"python": platform.python_version()},
        "project_head": "working-tree (uncommitted changes)",
        "evidence": ["communication.jsonl"],
    }
    communication = folder / "communication.jsonl"

    def log_reports(stage: str, status: Any) -> None:
        values = getattr(status, "values", {})
        for report_value in values.get("reports", ()):
            with communication.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {"time": utc_now(), "direction": "rx", "stage": stage, "source": "official_library", "data": report_value},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
    app: ApplicationRuntime | None = None
    adapter = None
    owner = f"kplusk-fxe-readonly-{uuid.uuid4().hex}"
    try:
        app = ApplicationRuntime((config,))
        app.leases.acquire(owner, (config.resource,), identified=False)
        report["stages"]["enumeration"] = {
            "state": "passed",
            "reason": "仅访问用户指定的当前 K+K 地址；不执行广播扫描",
        }
        adapter = create_adapter(config, app.visa)
        identity = adapter.identify()
        report["identity"] = jsonable(identity)
        report["stages"]["official_protocol_connection"] = {
            "state": "passed",
            "reason": "官方 K+K Library 由项目工厂打开 direct Ethernet 连接",
        }
        report["stages"]["compatibility_connection"] = {
            "state": "passed",
            "reason": "通过 registry.create_adapter 与项目型号工厂连接",
        }
        if config.expected_serial and identity.serial != config.expected_serial:
            report["stages"]["identity"] = {
                "state": "blocked",
                "reason": "官方 Library 只读接口未返回序列号，不能把配置序列号当成设备回报",
            }
        else:
            report["stages"]["identity"] = {
                "state": "blocked",
                "reason": "官方 Library 身份为 protocol_only，完整型号变体和序列号待 USB 工具/型号资料核对",
            }
        status = adapter.read_status()
        log_reports("initial_status", status)
        settings = adapter.read_settings(("firmware_version", "user_id", "report"))
        for reading in settings:
            if reading.operation == "report":
                for report_value in reading.value:
                    with communication.open("a", encoding="utf-8") as stream:
                        stream.write(
                            json.dumps(
                                {"time": utc_now(), "direction": "rx", "stage": "settings_report", "source": "official_library", "data": report_value},
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
        report["initial_snapshot"] = {"status": jsonable(status), "settings": jsonable(settings)}
        report["stages"]["parameter_read"] = {
            "state": "passed",
            "reason": "读取官方 firmware/user/report getter；报告保留为 raw_protocol_only",
        }
        report["stages"]["parameter_write"] = {
            "state": "not_tested",
            "reason": "兼容层只读，不调用任何设置 API",
        }
        report["stages"]["restoration"] = {
            "state": "not_tested",
            "reason": "没有修改参数，无恢复动作",
        }
        try:
            frequency_request = AcquisitionRequest(
                kind="scalar",
                channels=tuple(range(1, 25)),
                points=1,
                quantity="frequency",
            )
            frequency_batch = None
            frequency_error: Exception | None = None
            # A phase-average device needs two cumulative reports before its
            # read-only Hz estimate can be calculated.  Allow the first
            # report to arrive after the device's startup queue settles; this
            # never sends a device command.
            for attempt in range(16):
                try:
                    frequency_batch = adapter.acquire(frequency_request)
                    break
                except DeviceError as exc:
                    frequency_error = exc
                    if exc.code != "timeout" or attempt == 15:
                        raise
                    time.sleep(1.1)
            if frequency_batch is None:
                raise frequency_error or DeviceError("K+K frequency report unavailable", "timeout")
            report["initial_snapshot"]["frequency"] = jsonable(frequency_batch)
            source_report_type = frequency_batch.configuration.get("source_report_type", "RT_DOUBLE")
            frequency_reason = (
                "通过 Multi_GetReportStruct 读取 RT_PHASE 相邻帧并按设备时间戳计算 Hz；未发送设置命令"
                if frequency_batch.configuration.get("derived_from_phase")
                else "通过 Multi_GetReportStruct 读取完整 RT_DOUBLE 频率回报；未发送设置命令"
            )
            report["stages"]["frequency_read"] = {
                "state": "passed",
                "reason": frequency_reason,
                "source_report_type": source_report_type,
                "channels": [measurement.channel for measurement in frequency_batch.measurements],
                "values_hz": [measurement.values[0] for measurement in frequency_batch.measurements],
            }
            report_log = frequency_batch.configuration.get("report_log")
            if report_log:
                with communication.open("a", encoding="utf-8") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "time": utc_now(),
                                "direction": "rx",
                                "stage": "frequency_read",
                                "source": "official_library",
                                "data": report_log,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
        except Exception as exc:
            report["stages"]["frequency_read"] = {
                "state": "blocked" if getattr(exc, "code", "") == "timeout" else "failed",
                "reason": str(exc),
            }
        adapter.close()
        adapter = create_adapter(config, app.visa)
        reconnect_identity = adapter.identify()
        reconnect_status = adapter.read_status()
        log_reports("reconnection_status", reconnect_status)
        report["reconnection"] = {
            "identity": jsonable(reconnect_identity),
            "status": jsonable(reconnect_status),
            "identity_model_matches": reconnect_identity.model == identity.model,
        }
        report["stages"]["reconnection"] = {
            "state": "passed",
            "reason": "关闭兼容层会话后再次通过项目工厂打开并读回状态",
        }
        report["stages"]["physical_output"] = {
            "state": "not_tested",
            "reason": "未接外部测量仪器；软件报告不等于物理输出验收",
        }
        report["final_state"] = {
            "device_settings_changed": False,
            "pc_network_changed": False,
            "device_connection_closed": True,
        }
    except Exception as exc:
        report["error"] = str(exc)
        report["error_code"] = getattr(exc, "code", type(exc).__name__)
        report["stages"]["compatibility_connection"] = {
            "state": "blocked" if getattr(exc, "code", "") in {"resource_busy", "driver_missing", "timeout"} else "failed",
            "reason": str(exc),
        }
    finally:
        if adapter is not None:
            try:
                adapter.close()
            except Exception as exc:
                report["close_error"] = str(exc)
        if app is not None:
            try:
                app.leases.release(owner)
            finally:
                app.close()
        report["finished_at"] = utc_now()
        (folder / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / "record.md").write_text(_record_markdown(report, folder), encoding="utf-8")
        # Keep the raw result authoritative; the package converter only adds
        # the required derived summary and never participates in recovery.
        from .records import publish_report

        publish_report(folder)
    return report


def _record_markdown(report: dict[str, Any], folder: Path) -> str:
    stages = report["stages"]
    return (
        "# K+K FXE 项目兼容层只读批次\n\n"
        f"- 批次目录：`{folder.name}`\n"
        f"- 资源：`{report['resource']}`（经交换机）\n"
        "- 范围：通过项目注册、官方 K+K Library 工厂进行只读连接、身份元数据、二进制频率回报读取和重连；未写设置。\n\n"
        "## 结果\n\n"
        + "\n".join(f"- {name}: **{item['state']}** — {item.get('reason', '')}" for name, item in stages.items())
        + "\n\n官方报告原文在 `results.json` 的 snapshot/reconnection 字段中保留；有效 RT_DOUBLE 回报的通道值在 `initial_snapshot.frequency` 中保留。未返回完整回报时，频率阶段保持 blocked/failed，不把空帧当作通过。\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an explicit read-only K+K FXE compatibility test")
    parser.add_argument("resource", help="explicit K+K IP or kplusk:// endpoint")
    parser.add_argument("--expected-serial", default="")
    parser.add_argument("--folder", type=Path)
    args = parser.parse_args()
    root = application_home() / "hardware-tests"
    folder = args.folder or root / f"KPLUSK-FXE-UNKNOWN/{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-kplusk-compatibility-readonly"
    config = ConnectionConfig(args.resource, "kplusk.fxe", "kplusk", expected_serial=args.expected_serial)
    result = verify(config, folder)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
