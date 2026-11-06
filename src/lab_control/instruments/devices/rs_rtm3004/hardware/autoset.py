"""Authorized empty-input AUToscale qualification with an exact setup backup."""

from __future__ import annotations

import argparse
import hashlib
import platform
import time
import traceback
from datetime import datetime
from pathlib import Path

from lab_control.application.config import load_config
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError, jsonable, utc_now
from lab_control.instruments.common.hardware_paths import verify_runtime_paths
from lab_control.instruments.registry import create_adapter
from lab_control.storage.hardware_documents import refresh_after_batch
from lab_control.storage.runtime import hardware_lock_home, runtime_home
from lab_control.transport.base import block_payload, ieee_block

from ..adapter import Rtm3004Adapter
from .records import publish_report
from .verify import AuditTransport, differences, install_metadata_sinks, save, snapshot

SETUP_LIMIT = 16 * 1024 * 1024
EXTRA_QUERIES = (
    "TRIG:A:EDGE:COUP?",
    "TRIG:A:HYST?",
    "TRIG:A:EDGE:FILT:HFR?",
    "TRIG:A:EDGE:FILT:NREJ?",
    "TRIG:A:HOLD:MODE?",
    "TRIG:A:HOLD:TIME?",
    "TIM:ZOOM:STAT?",
    "TIM:ZOOM:SCAL?",
    "TIM:ZOOM:POS?",
    *(f"CALC:MATH{ch}:STAT?" for ch in range(1, 5)),
    *(f"REFC{ch}:STAT?" for ch in range(1, 5)),
    "WGEN:OUTP?",
)


def extended_snapshot(adapter):
    return {command: adapter.transport.query(command).decode("ascii").strip() for command in EXTRA_QUERIES}


def setup(adapter):
    bounded = getattr(adapter.transport, "query_binary", None)
    if not callable(bounded):
        raise DeviceError("Setup backup requires bounded blocks", "unsupported")
    return block_payload(bounded("SYST:SET?", SETUP_LIMIT))


def restore_setup(adapter, original):
    """The only binary write allowed by this test: the exact saved setup."""
    transport = adapter.transport
    assert isinstance(transport, AuditTransport) and transport.allow_writes
    transport.log(
        write="SYST:SET <original block>", bytes=len(original), sha256=hashlib.sha256(original).hexdigest()
    )
    transport.inner.write(b"SYST:SET " + ieee_block(original) + b"\n")
    if transport.query_timeout("*OPC?", 30).strip() != b"1":
        raise DeviceError("Setup restoration completion unconfirmed", "restoration_failed")
    error = transport.query("SYST:ERR?").decode().strip()
    if not error.lstrip("+").startswith("0,"):
        raise DeviceError("Setup restoration error: " + error, "restoration_failed")


def restore_transfer(adapter, initial):
    """Device Settings does not restore the SCPI transfer format on firmware 01.400."""
    for query, command, expected in (
        ("FORM?", "FORM", initial["format"]),
        ("FORM:BORD?", "FORM:BORD", initial["byte_order"]),
    ):
        actual = adapter.transport.query(query).decode("ascii").strip()
        if actual != expected:
            adapter.transport.write(command + " " + expected)
            if adapter.transport.query(query).decode("ascii").strip() != expected:
                raise DeviceError("Transfer restoration mismatch: " + query, "restoration_failed")
    error = adapter.transport.query("SYST:ERR?").decode("ascii").strip()
    if not error.lstrip("+").startswith("0,"):
        raise DeviceError("Transfer restoration error: " + error, "restoration_failed")


def verify_auto(config, folder, *, execute=False):
    import pyvisa

    report = {
        "adapter_id": "rs.rtm3004",
        "model": "RTM3004",
        "expected_serial": config.expected_serial,
        "resource": config.resource,
        "started_at": utc_now(),
        "hardware_accepted": False,
        "scope": "Auto setup completion, real readback, monitoring/recording lock and exact setup restoration; all analog inputs disconnected",
        "environment": {"python": platform.python_version(), "pyvisa": pyvisa.__version__},
        "source_sha256": {
            str(p.relative_to(Path(__file__).parents[1])): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path(__file__).parents[1].rglob("*.py")
            if "tests" not in p.parts
        },
        "stages": {
            key: "not_tested"
            for key in (
                "enumeration",
                "identity",
                "compatibility_connection",
                "parameter_read",
                "auto_setup",
                "post_auto_monitoring",
                "recording_lock",
                "restoration",
                "reconnection",
                "physical_output",
            )
        },
        "final_state": "No setting writes; backup pending",
        "operations": [],
    }
    plan = """# RTM3004 Auto 测试计划

仅核验配置中的一台 RTM3004 及完整序列号；四路输入无外接信号。使用正式进程/资产锁，初始化关闭。

1. 只读保存全部控制、格式、触发耦合/迟滞/滤波/保持、Zoom、数学/参考曲线和内部发生器输出状态；完整 SYST:SET? 备份及重复读取哈希一致后方可写入。
2. 显式初始化 REAL32，执行一次 Auto，30 秒有界等待 *OPC?，错误检查和实际参数/范围同步。保留拒绝、超时及未知状态，不重复 Auto。
3. 在原来未监测及监测后暂停的两种状态执行 Auto；每次后监测取得至少三帧完整有效回包。公共有界记录仅存元数据，验证记录期间 Auto 被锁定，停止后排空。
4. 停止软件监测，只在仍正常的会话中回写一次原始设置块，并分别恢复 FORM/FORM:BORD。核对原始设置 SHA256、逐项参数、输出状态，关闭重连再核对。失败不自动重放恢复。
5. 不执行复位、校准、网络修改或发生器输出命令。旧波形历史和关闭的菜单不作为可恢复内容；无信号不验收自动选档效果或物理精度。
"""
    (folder / "plan.md").write_text(plan, encoding="utf-8")
    save(folder / "results.json", report)
    app = session = adapter = None
    original = initial = extra = None
    wrote = False
    stage = "compatibility_connection"
    try:
        if config.adapter != "rs.rtm3004" or not config.expected_serial:
            raise ValueError("Select one RTM3004 and an exact serial")
        report["paths"] = verify_runtime_paths(runtime_home(), hardware_lock_home())
        app = ApplicationRuntime((config,), root=runtime_home())
        install_metadata_sinks(app, report)  # Both factories before the first frame.

        def factory(selected):
            obj = create_adapter(selected, app.visa)
            if not isinstance(obj, Rtm3004Adapter):
                obj.close()
                raise DeviceError("Adapter mismatch", "identity_conflict")
            obj.transport = AuditTransport(obj.transport, folder)
            return obj

        app.sessions.factory = factory
        device = app.add(config)
        report["device_id"] = device
        report["identity"] = jsonable(app.connect(device, configure_acquisition=False).result(60))
        report["stages"].update(identity="passed", compatibility_connection="passed")
        session = app.sessions.sessions[device]
        adapter = session.adapter
        assert isinstance(adapter, Rtm3004Adapter)

        def work(function, *args):
            assert session is not None
            return session.worker.submit(function, *args).result(60)

        stage = "parameter_read"
        initial = work(snapshot, adapter)
        extra = work(extended_snapshot, adapter)
        original = work(setup, adapter)
        (folder / "setup-original.bin").write_bytes(original)
        report.update(
            initial_snapshot=initial, extended_before=extra, setup_sha256=hashlib.sha256(original).hexdigest()
        )
        repeated = work(setup, adapter)
        report["setup_repeat_sha256"] = hashlib.sha256(repeated).hexdigest()
        if repeated != original:
            raise DeviceError("Setup backup is not stable; no writes", "backup_unstable")
        report["stages"][stage] = "passed"
        save(folder / "results.json", report)
        if not execute:
            report["final_state"] = "Readonly setup backup complete; no initialization or Auto"
            return report
        assert isinstance(adapter.transport, AuditTransport)
        adapter.transport.allow_writes = True
        report["preexisting_errors"] = []
        for _ in range(20):
            error = work(adapter.transport.query, "SYST:ERR?").decode().strip()
            report["preexisting_errors"].append(error)
            if error.lstrip("+").startswith("0,"):
                break
        else:
            raise DeviceError("Error queue did not drain", "preflight_failed")
        files_before = set(app.repository.root.rglob("*"))
        wrote = True  # Initialization can change format even if it subsequently fails.
        work(adapter.prepare_acquisition)
        service = app.monitoring
        for index in range(2):
            stage = "auto_setup"
            service.stop(device, preserve_intent=True).result(60)
            started = time.monotonic()
            result = app.sessions.submit(device, "apply", DeviceCommand("auto_setup", confirmed=True)).result(
                60
            )
            report["operations"].append(
                {
                    "at": utc_now(),
                    "command": "auto_setup",
                    "case": "not_monitoring" if index == 0 else "monitoring_paused",
                    "seconds": time.monotonic() - started,
                    "result": jsonable(result),
                    "after": work(snapshot, adapter),
                    "extended_after": work(extended_snapshot, adapter),
                }
            )
            save(folder / "results.json", report)
            if result.outcome != "success" or not result.control_update or not result.control_update.complete:
                raise DeviceError("Auto did not complete: " + result.error, "auto_failed")
            report["stages"][stage] = "passed"
            stage = "post_auto_monitoring"
            channels = tuple(ch for ch in range(1, 5) if work(adapter.read, "channel_enabled", ch))
            if not channels:
                raise DeviceError("No enabled channels after Auto", "auto_failed")
            request = AcquisitionRequest("waveform", channels)
            service.start(device, (request,), interval=0.5, record_interval=None, cadence="interval")
            task = service.tasks[device]
            start_sequence = task.sequence
            deadline = time.monotonic() + 30
            while task.sequence < start_sequence + 3 and time.monotonic() < deadline:
                if not task.running:
                    raise DeviceError("Post-Auto monitoring paused: " + task.error, "monitoring_failed")
                time.sleep(0.05)
            if task.sequence < start_sequence + 3:
                raise DeviceError("No stable post-Auto frames within 30 seconds", "monitoring_failed")
            report.setdefault("monitoring", []).append(
                {
                    "case": index,
                    "channels": channels,
                    "frames": task.sequence - start_sequence,
                    "timings": list(task.timings),
                }
            )
            report["stages"][stage] = "passed"
        stage = "recording_lock"
        service.set_recording(device, True)
        deadline = time.monotonic() + 30
        while task.recorder is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if task.recorder is None:
            raise DeviceError("Recorder did not start", "recording_failed")
        try:
            app.sessions.submit(device, "apply", DeviceCommand("auto_setup", confirmed=True))
        except DeviceError as exc:
            if exc.code != "task_busy":
                raise
        else:
            raise DeviceError("Recording failed to lock Auto", "lock_failed")
        while task.recorder.snapshot()["committed"] < 3 and time.monotonic() < deadline:
            if not task.running:
                raise DeviceError(task.error, "recording_failed")
            time.sleep(0.05)
        service.stop(device).result(60)
        task.recorder.finished.result(60)
        report["recording"] = task.recorder.snapshot()
        if (
            not report["recording"]["binary_done"]
            or report["recording"]["received"] != report["recording"]["committed"]
            or report["recording"]["committed"] < 3
        ):
            raise DeviceError("Metadata recording did not drain", "recording_failed")
        report["stages"][stage] = "passed"
        unexpected = [
            str(p)
            for p in set(app.repository.root.rglob("*")) - files_before
            if p.suffix.lower() in {".csv", ".npy", ".npz", ".png", ".bin"} or p.name.startswith("segment-")
        ]
        report["unexpected_curve_files"] = unexpected
        report["stages"]["metadata_only"] = "failed" if unexpected else "passed"
    except Exception as exc:
        report["error"] = repr(exc)
        report["traceback"] = traceback.format_exc()
        report["stages"][stage] = (
            "blocked"
            if getattr(exc, "code", "") in {"process_busy", "resource_busy", "connection_failed"}
            else "failed"
        )
    finally:
        if wrote and app is not None and session is not None and adapter is not None and original is not None:
            try:
                assert initial is not None and extra is not None
                app.monitoring.stop(device).result(60)
                if session.state != "connected":
                    raise DeviceError(
                        "Session uncertain; explicit recovery batch required; no restoration replay",
                        "restoration_failed",
                    )
                report["pre_restore_snapshot"] = work(snapshot, adapter)
                # Reading current state precedes this single original-block restoration.
                report["restoration_attempted_at"] = utc_now()
                save(folder / "results.json", report)
                work(restore_setup, adapter, original)
                work(restore_transfer, adapter, initial)
                observed = work(snapshot, adapter)
                if initial["state"] != "RUN" and observed["state"] == "RUN":
                    stopped = app.sessions.submit(
                        device, "apply", DeviceCommand("stop", confirmed=True)
                    ).result(60)
                    if stopped.outcome != "success":
                        raise DeviceError("Stop after setup restoration unconfirmed", "restoration_failed")
                report["final_snapshot"] = work(snapshot, adapter)
                report["extended_final"] = work(extended_snapshot, adapter)
                final_setup = work(setup, adapter)
                (folder / "setup-restored.bin").write_bytes(final_setup)
                report["restored_setup_sha256"] = hashlib.sha256(final_setup).hexdigest()
                report["restoration_differences"] = differences(initial, report["final_snapshot"])
                report["extended_differences"] = {
                    q: {"before": v, "after": report["extended_final"].get(q)}
                    for q, v in extra.items()
                    if report["extended_final"].get(q) != v
                }
                if (
                    report["restoration_differences"]
                    or report["extended_differences"]
                    or final_setup != original
                ):
                    raise DeviceError(
                        "Original setup or scalar restoration differs; no replay", "restoration_failed"
                    )
                report["stages"]["restoration"] = "passed"
                app.sessions.disconnect(device)
                report["reconnected_identity"] = jsonable(
                    app.connect(device, configure_acquisition=False).result(60)
                )
                session = app.sessions.sessions[device]
                adapter = session.adapter
                report["reconnected_snapshot"] = work(snapshot, adapter)
                if differences(initial, report["reconnected_snapshot"]) or work(setup, adapter) != original:
                    raise DeviceError("Reconnected setup differs", "readback_mismatch")
                report["stages"]["reconnection"] = "passed"
                report["final_state"] = (
                    "Exact original setup block and scalar/output states restored and reconnected; acquisition "
                    + report["reconnected_snapshot"]["state"]
                    + "; waveform history and menus not restored"
                )
            except Exception as exc:
                report["restoration_error"] = repr(exc)
                if report["stages"]["restoration"] != "passed":
                    report["stages"]["restoration"] = "failed"
                else:
                    report["stages"]["reconnection"] = "failed"
                report["final_state"] = (
                    "Restoration/reconnection not fully verified; see original setup and final observations; no automatic replay"
                )
        if app:
            app.close()
        report["finished_at"] = utc_now()
        save(folder / "results.json", report)
        (folder / "record.md").write_text(
            "# RTM3004 Auto 验证\n\n"
            + report["scope"]
            + "\n\n"
            + report["final_state"]
            + "\n\n计划见 plan.md；原始回包、设置块和阶段结果见本目录。无信号，不验收自动选档效果与物理精度。\n",
            encoding="utf-8",
        )
        publish_report(folder)
        refresh_after_batch(folder)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--execute", action="store_true", help="Run already-authorized Auto tests on disconnected inputs"
    )
    args = parser.parse_args()
    configs = load_config(args.config)
    if len(configs) != 1 or configs[0].adapter != "rs.rtm3004" or not configs[0].expected_serial:
        parser.error("Select one RTM3004 with an expected serial")
    from lab_control.storage.paths import filename_part

    folder = (
        runtime_home()
        / "hardware-tests"
        / ("RTM3004-" + filename_part(configs[0].expected_serial))
        / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-auto")
    )
    folder.mkdir(parents=True)
    result = verify_auto(configs[0], folder, execute=args.execute)
    print(
        {
            "folder": str(folder),
            "stages": result["stages"],
            "error": result.get("error"),
            "restoration_error": result.get("restoration_error"),
        }
    )


if __name__ == "__main__":
    main()
