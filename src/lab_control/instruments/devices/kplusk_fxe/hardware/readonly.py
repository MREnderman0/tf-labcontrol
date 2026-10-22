"""Explicit read-only acceptance through the production session, page and recorder."""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceError, jsonable, utc_now
from lab_control.storage.repository import application_home


def verify(
    config: ConnectionConfig,
    folder: Path,
    *,
    duration: float = 60,
    root: Path | None = None,
    input_condition: str = "未核对",
) -> dict[str, Any]:
    """One selected endpoint; no scan, setup or retries after acquisition failure."""
    folder.mkdir(parents=True, exist_ok=False)
    names = (
        "enumeration",
        "identity",
        "official_protocol_connection",
        "compatibility_connection",
        "parameter_read",
        "frequency_read",
        "monitoring",
        "recording",
        "display",
        "channel_selection",
        "snapshot_export",
        "parameter_write",
        "restoration",
        "reconnection",
        "physical_output",
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "adapter_id": config.adapter,
        "model": "K+K FXE (完整变体待核对)",
        "expected_serial": config.expected_serial,
        "resource": config.resource,
        "interface": "K+K direct Ethernet via switch",
        "started_at": utc_now(),
        "input_condition": input_condition,
        "scope": "正式会话、监测页、逐帧记录、选路、保存当前帧与重连；只读",
        "stages": {key: {"state": "not_tested"} for key in names},
        "environment": {
            "python": platform.python_version(),
            "executable": sys.executable,
            "frozen": bool(getattr(sys, "frozen", False)),
        },
        "project_head": "working-tree; executable/build manifest identifies deployed version",
        "evidence": ["communication.jsonl", "frames.jsonl"],
        "checks_passed": False,
        "hardware_accepted": False,
        "setting_write_api_calls": 0,
    }
    app = controller = page = None
    stage, device = "compatibility_connection", ""

    def passed(name, reason, **details):
        report["stages"][name] = {"state": "passed", "reason": reason, **details}

    def append(name, value):
        with (folder / name).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(jsonable(value), ensure_ascii=False, allow_nan=False) + "\n")

    def received(raw):
        content = raw.get_content()
        kind = raw.get_report_type()
        append(
            "communication.jsonl",
            {
                "time": utc_now(),
                "direction": "rx",
                "report_type": int(getattr(kind, "value", kind)),
                "header": raw.get_header(),
                "device_ms": raw.get_device_ms(),
                "content_hex": bytes(content).hex() if content else "",
                "log": raw.to_log_str(),
            },
        )

    try:
        if not 1 <= duration <= 3600:
            raise ValueError("duration must be 1..3600 seconds")
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QApplication, QWidget

        from lab_control.app import load_stylesheet, load_ui_font
        from lab_control.instruments.devices.kplusk_fxe.adapter import create
        from lab_control.storage.recording import read_frames
        from lab_control.ui.controller import WorkspaceController
        from lab_control.ui.pages.monitoring.frequency_counter import FrequencyCounterMonitoringPage

        qt = QApplication.instance() or QApplication([])
        assert isinstance(qt, QApplication)
        qt.setStyleSheet(load_stylesheet())
        qt.setFont(QFont(load_ui_font(), 10))
        app = ApplicationRuntime((config,), root=root)
        report["runtime_root"] = str(app.repository.root)

        def factory(selected):
            if (selected.resource, selected.adapter, selected.transport) != (
                config.resource,
                config.adapter,
                config.transport,
            ):
                raise DeviceError("Read-only acceptance selected another endpoint", "identity_conflict")
            adapter = create(selected)
            adapter.reports.on_report = received
            return adapter

        app.sessions.factory = factory
        device = app.add(config)
        identity = app.connect(device, configure_acquisition=False).result(50)
        report["identity"] = jsonable(identity)
        snapshot = app.sessions.capability_snapshot(device)
        if snapshot is None or snapshot.stale or not snapshot.channels:
            raise DeviceError(snapshot.error if snapshot else "No capabilities", "acquisition_failed")
        channels = tuple(snapshot.channels)
        report["capabilities"] = jsonable(snapshot)
        report["stages"]["enumeration"] = {"state": "not_tested", "reason": "仅连接指定地址，未扫描"}
        report["stages"]["identity"] = {
            "state": "blocked",
            "reason": "protocol_only；完整型号变体和序列号尚未从设备读回",
        }
        passed("official_protocol_connection", "官方库直连并读取完整测量报告")
        passed("compatibility_connection", "正式会话、进程锁及只读能力识别通过", channels=channels)
        stage = "parameter_read"
        status = app.sessions.submit(device, "status").result(10)
        readings = app.sessions.submit(device, "read_settings", (("firmware_version", "user_id"), 1)).result(
            10
        )
        report["initial_snapshot"] = {"status": jsonable(status), "settings": jsonable(readings)}
        passed(stage, "固件、用户号和状态查询完成，测量帧保留")
        parent = QWidget()
        controller = WorkspaceController(app, parent)
        page = FrequencyCounterMonitoringPage(controller)
        page.setWindowTitle("K+K 只读监测验收")
        page.resize(1400, 780)
        page.select_device(device)
        page.show()
        for check in page.channel_checks.values():
            check.setChecked(True)
        seen: set[tuple[str, int]] = set()
        checking_stream = True

        def tick():
            qt.processEvents()
            task = app.monitoring.snapshots().get(device, {})
            if checking_stream and task.get("error"):
                raise DeviceError(task["error"], "acquisition_failed")
            for frame in app.monitoring.recent(device, "scalar", "frequency"):
                key = (frame.session_id, frame.sequence)
                if key not in seen:
                    seen.add(key)
                    append(
                        "frames.jsonl",
                        {"sequence": frame.sequence, "session_id": frame.session_id, "batch": frame.batch},
                    )
            time.sleep(0.03)

        def wait(predicate, seconds=50):
            deadline = time.monotonic() + seconds
            while not predicate():
                if time.monotonic() >= deadline:
                    raise DeviceError("只读验收等待超时", "timeout")
                tick()
            tick()

        def finish(future, seconds=60):
            wait(future.done, seconds)
            return future.result()

        stage = "frequency_read"
        page.start()
        wait(lambda: app.monitoring.latest(device, "scalar", "frequency") is not None)
        frame = app.monitoring.latest(device, "scalar", "frequency")
        assert frame is not None
        report["initial_snapshot"]["frequency"] = jsonable(frame.batch)
        passed(
            stage,
            "首次正式监测直接取得有效帧，无失败后重试",
            channels=channels,
            values_hz=[m.values[0] for m in frame.batch.measurements],
        )
        stage = "recording"
        page.record()
        started = time.monotonic()
        while time.monotonic() - started < duration:
            tick()
        elapsed = time.monotonic() - started
        page.refresh_frame(device)
        if page.plot.displayed_frame is None or {key[0] for key in page.plot.curves} != set(channels):
            raise DeviceError("监测页未显示全部所选通道", "display_failed")
        page.grab().save(str(folder / "monitoring.png"))
        report["evidence"].append("monitoring.png")
        passed("display", "实时数值与曲线已渲染，截图留档", channels=channels)
        finish(app.monitoring.stop(device))
        recorder = app.monitoring.tasks[device].recorder
        if recorder is None:
            raise DeviceError("Recording did not start", "recording_failed")
        paths = finish(recorder.finish())
        saved = [frame for path in paths for frame in read_frames(Path(path))]
        info = recorder.snapshot()
        if (
            not saved
            or info["received"] != info["committed"]
            or len(saved) != info["committed"]
            or info["converted"] != info["committed"]
            or info.get("error")
            or info.get("csv_error")
            or any({m["channel"] for m in frame["measurements"]} != set(channels) for frame in saved)
        ):
            raise DeviceError("逐帧记录数量、通道或 CSV 核验失败", "recording_failed")
        passed("monitoring", "连续监测结束，无错误或自动暂停", elapsed_seconds=elapsed, frames=len(seen))
        passed("recording", "二进制和 CSV 帧数一致，每帧包含所选通道", **info, paths=paths)
        stage = "snapshot_export"
        exported = finish(app.monitoring.export_displayed_frame(device, page.plot.displayed_frame, "general"))
        passed(stage, "保存已显示帧完成，未重新读取", path=str(exported))
        stage = "channel_selection"
        selected_channels = channels[::2]
        for channel, check in page.channel_checks.items():
            check.setChecked(channel in selected_channels)
        page.start()
        wait(
            lambda: (
                (current := app.monitoring.latest(device, "scalar", "frequency")) is not None
                and tuple(m.channel for m in current.batch.measurements) == selected_channels
            )
        )
        page.refresh_frame(device)
        if {key[0] for key in page.plot.curves} != set(selected_channels):
            raise DeviceError("选路后的显示通道不匹配", "display_failed")
        page.grab().save(str(folder / "selected-channels.png"))
        report["evidence"].append("selected-channels.png")
        finish(app.monitoring.stop(device))
        passed(stage, "停止后修改软件选路，再次监测及显示通过", channels=selected_channels)
        stage = "reconnection"
        # The scheduler correctly marks the old task disconnected while its
        # session is absent. This intentional gap is not an acquisition failure.
        checking_stream = False
        app.sessions.disconnect(device)
        finish(app.connect(device, configure_acquisition=False))
        controller.poll()
        page.select_device(device)
        page.start()
        checking_stream = True
        wait(lambda: app.monitoring.latest(device, "scalar", "frequency") is not None)
        reconnected = app.monitoring.latest(device, "scalar", "frequency")
        assert reconnected is not None
        report["reconnection"] = jsonable(reconnected.batch)
        finish(app.monitoring.stop(device))
        passed(stage, "释放会话后重新连接并取得有效读数，软件选路保留")
        report["checks_passed"] = True
    except Exception as exc:
        report["error"], report["error_code"] = str(exc), getattr(exc, "code", type(exc).__name__)
        report["stages"][stage] = {
            "state": "blocked" if report["error_code"] in {"resource_busy", "driver_missing"} else "failed",
            "reason": str(exc),
        }
    finally:
        if controller is not None:
            controller.close()
        if app is not None:
            try:
                app.close()
            except Exception as exc:
                report["close_error"] = str(exc)
                report["checks_passed"] = False
        if page is not None:
            page.close()
        for key, reason in (
            ("parameter_write", "只读，未调用设备设置 API"),
            ("restoration", "未修改参数，无恢复动作"),
            ("physical_output", "输入条件另列；未接已知信号或外部测量仪器，不作精度和物理验收"),
        ):
            report["stages"][key] = {"state": "not_tested", "reason": reason}
        report["final_state"] = {
            "device_settings_changed": False,
            "pc_network_changed": False,
            "device_connection_closed": not bool(report.get("close_error")),
            "input_condition": input_condition,
        }
        report["finished_at"] = utc_now()
        (folder / "results.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (folder / "record.md").write_text(
            "# K+K FXE 正式监测只读验收\n\n"
            + f"- 资源：`{config.resource}`\n- 输入条件：{input_condition}\n"
            + "- 不扫描、不写参数、不改网络；数据链路通过不代表精度或物理验收。\n\n"
            + "| 项目 | 结果 | 依据 |\n| --- | --- | --- |\n"
            + "\n".join(
                f"| {key} | {value['state']} | {value.get('reason', '')} |"
                for key, value in report["stages"].items()
            )
            + "\n\n原始回包见 communication.jsonl；帧见 frames.jsonl；记录路径和数量见 results.json。\n",
            encoding="utf-8",
        )
        from .records import publish_report

        publish_report(folder)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("resource")
    parser.add_argument("--expected-serial", default="")
    parser.add_argument("--folder", type=Path)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--input-condition", default="未核对")
    args = parser.parse_args()
    folder = args.folder or application_home() / "hardware-tests" / "KPLUSK-FXE-UNKNOWN" / (
        datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-kplusk-monitoring-readonly"
    )
    config = ConnectionConfig(args.resource, "kplusk.fxe", "kplusk", expected_serial=args.expected_serial)
    result = verify(config, folder, duration=args.duration, input_condition=args.input_condition)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
