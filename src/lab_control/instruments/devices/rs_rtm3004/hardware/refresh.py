"""Empty/frozen/live waveform regression on one already-authorized RTM3004."""

import hashlib
import platform
import time
import traceback
from pathlib import Path

from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, jsonable, utc_now
from lab_control.instruments.common.hardware_paths import verify_runtime_paths
from lab_control.instruments.registry import create_adapter
from lab_control.storage.hardware_documents import refresh_after_batch
from lab_control.storage.runtime import hardware_lock_home, runtime_home

from ..adapter import Rtm3004Adapter
from .autoset import extended_snapshot, restore_transfer, setup
from .records import publish_report
from .verify import AuditTransport, differences, frame_metadata, install_metadata_sinks, save, snapshot


def verify_refresh(config, folder, *, timebase=None):
    """Requires permission to run acquisition; never changes channel enable/scales."""
    import pyvisa

    report = {
        "adapter_id": "rs.rtm3004",
        "model": "RTM3004",
        "expected_serial": config.expected_serial,
        "resource": config.resource,
        "started_at": utc_now(),
        "hardware_accepted": False,
        "scope": "Empty-input regression: stopped empty channel pauses without reconnect; explicit RUN produces changing frames; STOP produces frozen frames; restore controls and reconnect. Waveform history is replaced.",
        "environment": {"python": platform.python_version(), "pyvisa": pyvisa.__version__},
        "source_sha256": {
            str(p.relative_to(Path(__file__).parents[1])): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path(__file__).parents[1].rglob("*.py")
            if "tests" not in p.parts
        },
        "stages": {
            k: "not_tested"
            for k in (
                "enumeration",
                "identity",
                "compatibility_connection",
                "parameter_read",
                "parameter_write",
                "empty_channel",
                "live_refresh",
                "recording",
                "frozen_waveform",
                "restoration",
                "reconnection",
                "physical_output",
            )
        },
        "final_state": "No writes; backup pending",
    }
    save(folder / "results.json", report)
    (folder / "plan.md").write_text(
        "# 波形刷新回归\n\n只连接指定序列号；初始化关闭，先完整备份。保留当前无数据通道状态，验证读取暂停且不重连、不保存空帧。显式 RUN 后连续观察至少六帧及逐通道哈希变化，元数据记录并排空。显式 STOP 后两次读取验证冻结提示与内容一致。恢复原控制、格式和运行语义，关闭重连核验。无信号不验收物理精度；波形历史无法恢复。",
        encoding="utf-8",
    )
    app = adapter = session = None
    initial = extra = None
    wrote = False
    stage = "compatibility_connection"
    try:
        if config.adapter != "rs.rtm3004" or not config.expected_serial:
            raise ValueError("Select one serial-bound RTM3004")
        report["paths"] = verify_runtime_paths(runtime_home(), hardware_lock_home())
        if timebase is not None:
            with (folder / "plan.md").open("a", encoding="utf-8") as plan:
                plan.write(f"\n\n本批在备份后临时设置时基 {timebase} s/格，结束前恢复原时基并核对。\n")
        app = ApplicationRuntime((config,), root=runtime_home())
        install_metadata_sinks(app, report)

        def factory(cfg):
            obj = create_adapter(cfg, app.visa)
            assert isinstance(obj, Rtm3004Adapter)
            obj.transport = AuditTransport(obj.transport, folder)
            return obj

        app.sessions.factory = factory
        key = app.add(config)
        report["identity"] = jsonable(app.connect(key, configure_acquisition=False).result(60))
        report["stages"].update(identity="passed", compatibility_connection="passed")
        session = app.sessions.sessions[key]
        adapter = session.adapter
        assert isinstance(adapter, Rtm3004Adapter)

        def work(fn, *args):
            assert session is not None
            return session.worker.submit(fn, *args).result(60)

        def apply(name, value=None):
            assert app is not None
            result = app.sessions.submit(
                key, "apply", DeviceCommand(name, {} if value is None else {"value": value}, confirmed=True)
            ).result(60)
            report.setdefault("operations", []).append({"operation": name, "result": jsonable(result)})
            assert result.outcome == "success", result.error
            report["stages"]["parameter_write"] = "passed"

        stage = "parameter_read"
        initial = work(snapshot, adapter)
        extra = work(extended_snapshot, adapter)
        report.update(initial_snapshot=initial, extended_before=extra)
        original = work(setup, adapter)
        (folder / "setup-original.bin").write_bytes(original)
        report["setup_sha256"] = hashlib.sha256(original).hexdigest()
        channels = tuple(ch for ch in range(1, 5) if initial["values"][f"{ch}.channel_enabled"])
        report["headers_before"] = {
            str(ch): work(adapter.transport.query, f"CHAN{ch}:DATA:HEAD?").decode().strip() for ch in channels
        }
        report["stages"][stage] = "passed"
        report["preexisting_errors"] = []
        for _ in range(20):
            error = work(adapter.transport.query, "SYST:ERR?").decode().strip()
            report["preexisting_errors"].append(error)
            if error.lstrip("+").startswith("0,"):
                break
        else:
            raise RuntimeError("Error queue did not drain; no writes")
        save(folder / "results.json", report)
        assert isinstance(adapter.transport, AuditTransport)
        adapter.transport.allow_writes = True
        wrote = True
        work(adapter.prepare_acquisition)
        request = AcquisitionRequest("waveform", channels)
        service = app.monitoring
        files_before = set(app.repository.root.rglob("*"))
        if any(
            tuple(map(float, raw.split(","))) == (0, 0, 0, 0) for raw in report["headers_before"].values()
        ):
            stage = "empty_channel"
            with service.lock:
                service.start(key, (request,), interval=0.1, record_interval=None)
                preparation = service.set_recording(key, True, background=True)
            assert preparation is not None
            preparation.result(60)
            task = service.tasks[key]
            deadline = time.monotonic() + 20
            while task.running and time.monotonic() < deadline:
                time.sleep(0.05)
            empty_notice, empty_error = task.notice, task.error
            service.stop(key).result(60)
            assert task.recorder is not None
            task.recorder.finished.result(60)
            report["empty_result"] = {
                "notice": empty_notice,
                "error": empty_error,
                "recovery": jsonable(task.recovery),
                "sequence": task.sequence,
                "recording": task.recorder.snapshot(),
                "session_state": session.state,
            }
            assert "尚无波形" in empty_notice and not empty_error and task.recovery is None
            assert task.sequence == 0 and task.recorder.received == task.recorder.committed == 0
            assert session.state == "connected"
            report["stages"][stage] = "passed"
        stage = "live_refresh"
        if timebase is not None:
            report["requested_timebase"] = timebase
            apply("timebase", timebase)
        apply("run")
        with service.lock:
            service.start(key, (request,), interval=0.3, record_interval=None)
            preparation = service.set_recording(key, True, background=True)
        assert preparation is not None
        preparation.result(60)
        task = service.tasks[key]
        report["frames"] = []
        sequence = -1
        deadline = time.monotonic() + 45
        while len(report["frames"]) < 6 and time.monotonic() < deadline:
            frame = service.latest(key, "waveform")
            if frame is not None and frame.sequence != sequence:
                sequence = frame.sequence
                report["frames"].append(
                    {
                        "sequence": sequence,
                        "at": frame.batch.timestamp,
                        "settings_timestamps": dict(frame.batch.settings_timestamps),
                        **frame_metadata(frame.batch),
                    }
                )
            if not task.running or task.recovery is not None:
                raise RuntimeError("Live monitor stopped/reconnected: " + str(service.snapshots()[key]))
            time.sleep(0.05)
        service.stop(key).result(60)
        assert task.recorder is not None
        task.recorder.finished.result(60)
        assert len(report["frames"]) >= 6
        report["distinct_hashes"] = {
            str(ch): len({c["sha256"] for f in report["frames"] for c in f["channels"] if c["channel"] == ch})
            for ch in channels
        }
        assert all(n >= 2 for n in report["distinct_hashes"].values())
        assert all(f["performance"]["instrument_running"] for f in report["frames"])
        stable_frames = [
            f
            for f in report["frames"]
            if not any(
                c["metadata"]["growing_prefix"] or c["metadata"]["sweep_rollover"] for c in f["channels"]
            )
        ]
        if getattr(adapter, "supports_stream_acquisition", False) and stable_frames:
            cached = [f for f in report["frames"] if f["validation"] == "cached"]
            assert cached, "No cached steady frames observed"
            baseline = None
            for frame in report["frames"]:
                if frame["validation"] == "full":
                    baseline = frame["settings_timestamps"]
                else:
                    assert baseline == frame["settings_timestamps"], (
                        "Cached readings changed their provenance"
                    )
            report["cache_queries"] = {
                "full": [f["performance"]["queries"] for f in report["frames"] if f["validation"] == "full"],
                "cached": [f["performance"]["queries"] for f in cached],
            }
            report["stages"]["stream_context"] = "passed"
        report["stages"][stage] = "passed"
        stage = "recording"
        report["recording"] = task.recorder.snapshot()
        assert (
            task.recorder.committed >= 6
            and task.recorder.received == task.recorder.committed
            and task.recorder.binary_done
        )
        report["curve_files"] = [
            str(p)
            for p in set(app.repository.root.rglob("*")) - files_before
            if p.suffix.lower() in {".csv", ".npy", ".npz", ".png", ".bin"} or p.name.startswith("segment-")
        ]
        assert not report["curve_files"]
        report["stages"][stage] = "passed"
        stage = "frozen_waveform"
        apply("stop")
        frozen = [app.sessions.submit(key, "acquire", request).result(60) for _ in range(2)]
        report["frozen_frames"] = [frame_metadata(b) for b in frozen]
        assert all(b.performance["instrument_running"] is False for b in frozen)
        assert [c["sha256"] for c in report["frozen_frames"][0]["channels"]] == [
            c["sha256"] for c in report["frozen_frames"][1]["channels"]
        ]
        report["stages"][stage] = "passed"
    except Exception as exc:
        report["error"] = repr(exc)
        report["traceback"] = traceback.format_exc()
        report["stages"][stage] = (
            "blocked" if getattr(exc, "code", "") in {"process_busy", "resource_busy"} else "failed"
        )
    finally:
        if wrote and app is not None and session is not None and adapter is not None and initial is not None:
            try:
                app.monitoring.stop(key).result(60)
                assert session.state == "connected", "Uncertain session; no blind restoration"
                current = work(snapshot, adapter)
                if current["values"]["1.timebase"] != initial["values"]["1.timebase"]:
                    apply("timebase", initial["values"]["1.timebase"])
                if (current["state"] == "RUN") != (initial["state"] == "RUN"):
                    apply("run" if initial["state"] == "RUN" else "stop")
                work(restore_transfer, adapter, initial)
                report["final_snapshot"] = work(snapshot, adapter)
                report["extended_final"] = work(extended_snapshot, adapter)
                report["restoration_differences"] = differences(initial, report["final_snapshot"])
                assert not report["restoration_differences"] and extra == report["extended_final"]
                report["stages"]["restoration"] = "passed"
                app.sessions.disconnect(key)
                report["reconnected_identity"] = jsonable(
                    app.connect(key, configure_acquisition=False).result(60)
                )
                session = app.sessions.sessions[key]
                adapter = session.adapter
                report["reconnected_snapshot"] = work(snapshot, adapter)
                assert not differences(initial, report["reconnected_snapshot"])
                report["stages"]["reconnection"] = "passed"
                report["final_state"] = (
                    "Queried controls, transfer format, acquisition state and generator output restored; reconnected. New four-channel waveform history replaces old history. Acquisition "
                    + report["reconnected_snapshot"]["state"]
                )
            except Exception as exc:
                report["restoration_error"] = repr(exc)
                report["stages"]["restoration"] = "failed"
                report["final_state"] = (
                    "Restoration not fully verified; no blind retries. Consult final observations."
                )
        if app:
            app.close()
        report["finished_at"] = utc_now()
        save(folder / "results.json", report)
        (folder / "record.md").write_text(
            "# 波形刷新回归\n\n" + report["scope"] + "\n\n" + report["final_state"], encoding="utf-8"
        )
        publish_report(folder)
        refresh_after_batch(folder)
    return report
