"""Explicitly opted-in, identity-bound full/cached comparison; never writes curves."""

from __future__ import annotations

import hashlib
import platform
import time
import traceback
from pathlib import Path
from typing import Any, cast

import numpy as np

from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError, jsonable, utc_now
from lab_control.instruments.registry import create_adapter
from lab_control.storage.recording import Recording
from lab_control.storage.repository import application_home

from ..adapter import WaveRunner9404MAdapter, equivalent
from .records import publish_report
from .validate_control import AuditTransport, Verification, native_paths, save


def frame_record(batch) -> dict[str, Any]:
    return {
        "timestamp": batch.timestamp,
        "validation": batch.validation,
        "performance": batch.performance,
        "settings_timestamps": batch.settings_timestamps,
        "channels": [
            {
                "channel": m.channel,
                "points": len(m.values),
                "metadata": m.metadata,
                "data_hash": hashlib.sha256(np.asarray(m.values).tobytes()).hexdigest(),
                "axis_hash": hashlib.sha256(np.asarray(m.axis).tobytes()).hexdigest(),
            }
            for m in batch.measurements
        ],
    }


def statistics(entries) -> dict[str, Any]:
    def distribution(values):
        return (
            {
                "median": float(np.median(values)),
                "p95": float(np.percentile(values, 95)),
                "maximum": float(np.max(values)),
            }
            if values
            else {}
        )

    perf = [entry["performance"] for entry in entries]
    keys: dict[str, list[str]] = {}
    for entry in entries:
        for channel in entry["channels"]:
            token = channel["metadata"].get("acquisition_key")
            if token is not None:
                keys.setdefault(str(channel["channel"]), []).append(token)
    return {
        "frames": len(entries),
        "queries": sorted({p["queries"] for p in perf}),
        "received_bytes": sum(p["received_bytes"] for p in perf),
        **{
            name: distribution([p[name] for p in perf])
            for name in (
                "acquisition_seconds",
                "read_seconds",
                "write_seconds",
                "parse_seconds",
                "queue_seconds",
            )
        },
        "frame_intervals": distribution(
            [b["received_at_monotonic"] - a["received_at_monotonic"] for a, b in zip(perf, perf[1:])]
        ),
        "acquisitions": {
            ch: {
                "identifiable_reads": len(tokens),
                "distinct_tokens": len(set(tokens)),
                "repeat_ratio": 1 - len(set(tokens)) / len(tokens),
            }
            for ch, tokens in keys.items()
        },
    }


def qualifies(baseline, candidate) -> bool:
    a, b = baseline["acquisition_seconds"], candidate["acquisition_seconds"]
    return b["median"] <= a["median"] * 0.8 and b["p95"] <= a["p95"]


class MetadataTransport(AuditTransport):
    """Retain complete text exchanges and hashes/lengths for binary evidence."""

    def read(self):
        try:
            raw = self.inner.read()
            if ":WF?" in self.last:
                self.log(direction="rx", bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            else:
                self.log(direction="rx", text=raw.decode("ascii", errors="backslashreplace"))
            return raw
        except Exception as exc:
            self.log(direction="error", error=str(exc))
            raise


class MetadataRecording(Recording):
    def _csv_loop(self):
        self.thread.join()
        self.finished.set_result([])
        self.wake()


def install_metadata_sinks(app, report):
    """Must run before connect/first capture. Reuse reservation, queue and drain."""

    def forbidden_export(*args, **kwargs):
        raise AssertionError("Curve export forbidden in a performance batch")

    def factory(mode, **options):
        recorder = MetadataRecording(app.repository.root, mode, **options)

        def append(sequence, batch):
            report.setdefault("saved_frames", []).append({"sequence": sequence, **frame_record(batch)})
            return "metadata-only"

        setattr(recorder.segments, "append", append)
        return recorder

    app.repository.export = forbidden_export
    app.repository.create_recording = factory


def verify(config, folder: Path, *, rounds=3, frames=20, duration=60) -> dict[str, Any]:
    """Caller confirms empty inputs; no retries of acquisitions or setting writes."""
    import pyvisa

    (folder / "communication.jsonl").touch(exist_ok=True)
    report: dict[str, Any] = dict(
        adapter_id="lecroy.9404m",
        started_at=utc_now(),
        resource=config.resource,
        expected_serial=config.expected_serial,
        config=jsonable(config),
        scope="Empty-input WORD full/cached and chunk comparison; AUTO metadata-only scheduling",
        environment={"python": platform.python_version(), "pyvisa": pyvisa.__version__},
        operations=[],
        skipped=[],
        groups={},
        frames=[],
        stages={"physical_output": "not_tested"},
        final_state="No writes yet",
        active_stage="connection",
        source_sha256={
            str(p.relative_to(Path(__file__).parents[1])): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path(__file__).parents[1].rglob("*.py")
            if "tests" not in p.parts
        },
    )
    app = test = original = None
    mutated = False
    transcripts = []
    group = "setup"

    def stage(name):
        nonlocal group
        group = name
        report["active_stage"] = name
        report["stages"][name] = "not_tested"
        save(folder / "results.json", report)

    def done():
        report["stages"][group] = "passed"
        save(folder / "results.json", report)

    try:
        if config.adapter != "lecroy.9404m" or config.transport != "visa" or not config.expected_serial:
            raise DeviceError("Require exact VISA model and serial", "identity_required")
        if rounds < 3 or frames < 20 or duration < 60:
            raise ValueError("Qualification requires 3 rounds, 20 measured frames, 60 s scheduling")
        report["physical_paths"] = native_paths(application_home())
        app = ApplicationRuntime((config,), root=folder / "runtime", hardware=True)
        install_metadata_sinks(app, report)
        acquire = app.sessions._acquire

        def observed(*args, **kwargs):
            batch = acquire(*args, **kwargs)
            report["frames"].append(
                {"group": group, "observed_at": time.perf_counter(), **frame_record(batch)}
            )
            return batch

        setattr(app.sessions, "_acquire", observed)

        def factory(selected):
            if selected != config:
                raise DeviceError("Only the selected asset is authorized", "identity_conflict")
            adapter = create_adapter(selected, app.visa)
            assert isinstance(adapter, WaveRunner9404MAdapter)
            adapter.supports_live_acquisition = adapter.supports_stream_acquisition = True
            allowed = {"*IDN?"} | {
                s.query.format(ch=ch)
                for s in adapter.profile.commands.values()
                if s.query
                for ch in (1, 2, 3, 4)
            }
            transport = MetadataTransport(adapter.transport, folder, allowed)
            transcripts.append(transport)
            adapter.transport = transport
            return adapter

        app.sessions.factory = factory
        key = app.add(config)
        identity = app.connect(key, configure_acquisition=False).result(120)
        report["environment"]["visa"] = str(app.visa.manager.visalib)
        report["identity"] = jsonable(identity)
        report["stages"].update(connection="passed", identity="passed")
        test = Verification(app, key, folder, report)
        stage("backup")
        original = test.snapshot()
        report["initial_snapshot"] = original
        save(folder / "initial_snapshot.json", original)
        done()
        stage("prepare")
        cast(MetadataTransport, test.adapter.transport).writes_allowed = True
        mutated = True
        test.call(test.adapter.prepare_acquisition)
        # Complete transfer, same sampling/memory/timebase in every comparison.
        test.call(lambda: test.adapter.transport.write("WFSU SP,0,NP,0,FP,0,SN,0"))
        for ch in (1, 2, 3, 4):
            if original[f"{ch}.channel_enabled"] != "ON":
                test.apply("channel_enabled", "ON", ch)
        test.apply("run")
        time.sleep(1)
        test.apply("stop")
        done()
        reference: dict[int, tuple[str, str]] = {}

        def measure(name, channels, mode):
            stage(name)
            request = AcquisitionRequest("waveform", channels, consistency=mode, context_id=name)
            for index in range(frames + 1):
                batch = app.sessions.submit(key, "acquire", request).result(120)
                entry = report["frames"][-1]
                entry["warmup"] = index == 0
                for channel in entry["channels"]:
                    hashes = channel["data_hash"], channel["axis_hash"]
                    expected = reference.setdefault(channel["channel"], hashes)
                    if hashes != expected:
                        raise DeviceError("STOP samples/axis differ between paths", "data_mismatch")
                if (
                    index
                    and mode != "full"
                    and (batch.validation != "cached" or batch.performance["queries"] != len(channels))
                ):
                    raise DeviceError("Stable path added queries or lost cache", "cache_failed")
            entries = [e for e in report["frames"] if e["group"] == name and not e["warmup"]]
            report["groups"][name] = statistics(entries)
            done()

        for channels in ((1,), (1, 2, 3, 4)):
            for round_index in range(rounds):
                # Alternate order so warm device/network trends do not always favor caching.
                for mode in ("full", "live") if round_index % 2 == 0 else ("live", "full"):
                    measure(f"{len(channels)}ch-r{round_index + 1}-{mode}", channels, mode)
        report["cache_performance_passed"] = all(
            qualifies(report["groups"][f"{n}ch-r{r}-full"], report["groups"][f"{n}ch-r{r}-live"])
            for n in (1, 4)
            for r in range(1, rounds + 1)
        )
        handle = cast(MetadataTransport, test.adapter.transport).inner.handle
        default_chunk = handle.chunk_size
        report["default_chunk_size"] = default_chunk
        try:
            for round_index in range(rounds):
                for size in (default_chunk, 65536, 262144):
                    test.call(lambda value=size: setattr(handle, "chunk_size", value))
                    measure(f"chunk-{size}-r{round_index + 1}", (1, 2, 3, 4), "live")
        finally:
            test.call(lambda: setattr(handle, "chunk_size", default_chunk))
        report["chunk_qualified"] = {
            str(size): all(
                qualifies(
                    report["groups"][f"chunk-{default_chunk}-r{r}"], report["groups"][f"chunk-{size}-r{r}"]
                )
                for r in range(1, rounds + 1)
            )
            for size in (65536, 262144)
        }
        test.apply("run")
        for recording in (False, True):
            stage("auto_recording" if recording else "auto_monitoring")
            app.monitoring.start(
                key,
                (AcquisitionRequest("waveform", (1, 2, 3, 4)),),
                interval=0.5,
                record_interval=1.0,
                cadence="fastest",
            )
            task = app.monitoring.tasks[key]
            if recording:
                app.monitoring.set_recording(key, True)
                try:
                    app.sessions.submit(key, "apply", DeviceCommand("timebase", {"value": 0.001}))
                except DeviceError as exc:
                    if exc.code != "task_busy":
                        raise
                else:
                    raise AssertionError("Recording did not lock parameters")
            deadline = time.monotonic() + duration
            while time.monotonic() < deadline:
                if not task.running:
                    raise DeviceError(str(app.monitoring.snapshots()), "monitoring_failed")
                time.sleep(0.1)
            app.monitoring.stop(key).result(120)
            if recording:
                assert task.recorder is not None
                task.recorder.finished.result(120)
                status = task.recorder.snapshot()
                report["metadata_recording"] = status
                if status["received"] != status["committed"] or status["pending"] or status["error"]:
                    raise AssertionError("Metadata recording failed to drain")
            entries = [e for e in report["frames"] if e["group"] == group]
            report["groups"][group] = statistics(entries)
            report.setdefault("scheduling", {})[group] = jsonable(app.monitoring.snapshots())
            if not entries or any(
                e["performance"]["queries"] != 4 for e in entries if e["validation"] == "cached"
            ):
                raise AssertionError("Scheduling inserted queries into cached acquisitions")
            if app.sessions.snapshots()[0]["controlsLocked"]:
                raise AssertionError("Parameter lock not released after drain")
            done()
    except Exception as exc:
        report.update(
            error=str(exc),
            error_code=getattr(exc, "code", type(exc).__name__),
            error_traceback=traceback.format_exc(),
        )
        report["stages"][report["active_stage"]] = "failed"
    finally:
        restore_verified = False
        if test and original is not None:
            assert app is not None
            try:
                app.monitoring.stop(test.device_id).result(120)
                if app.sessions.sessions[test.device_id].state in {"error", "degraded"}:
                    app.sessions.disconnect(test.device_id)
                    app.connect(test.device_id, configure_acquisition=False).result(120)
                    cast(MetadataTransport, test.adapter.transport).writes_allowed = mutated
                if mutated:
                    test.restore_parameters(original)
                final = test.snapshot()
                report["final_snapshot"] = final
                delta = {
                    k: {"before": v, "after": final[k]}
                    for k, v in original.items()
                    if not equivalent(v, final[k])
                }
                report["restoration_differences"] = delta
                save(folder / "restoration-differences.json", delta)
                save(folder / "final_snapshot.json", final)
                if delta:
                    raise DeviceError("Final snapshot differs", "restoration_failed")
                report["stages"]["restoration"] = "passed"
                restore_verified = True
                report["final_state"] = (
                    "Original settings, transfer configuration and acquisition mode restored and verified"
                )
                report["reconnections"] = []
                for index in range(3):
                    app.sessions.disconnect(test.device_id)
                    actual = app.connect(test.device_id, configure_acquisition=False).result(120)
                    if actual.key != identity.key or test.snapshot() != final:
                        raise DeviceError("Reconnect identity/settings differ", "reconnection_failed")
                    report["reconnections"].append({"attempt": index + 1, "identity": jsonable(actual)})
                report["stages"]["reconnection"] = "passed"
            except Exception as exc:
                if restore_verified:
                    report["reconnection_error"] = str(exc)
                    report["stages"]["reconnection"] = "failed"
                    report["final_state"] += "; subsequent read-only reconnection failed: " + str(exc)
                else:
                    report["restoration_error"] = str(exc)
                    report["stages"]["restoration"] = "failed"
                    report["final_state"] = "Final state requires review: " + str(exc)
        if app:
            try:
                app.close()
                report["session_released"] = True
            except Exception as exc:
                report["close_error"] = str(exc)
        forbidden = [
            str(p.relative_to(folder))
            for p in folder.rglob("*")
            if p.is_file() and (p.suffix in {".csv", ".bin", ".npy", ".npz"} or p.name.startswith("segment-"))
        ]
        report["curve_files"] = forbidden
        report["stages"]["metadata_only"] = "failed" if forbidden else "passed"
        report["qualified"] = bool(
            report.get("cache_performance_passed")
            and not forbidden
            and not report.get("error")
            and not report.get("restoration_error")
            and not report.get("reconnection_error")
            and not report.get("close_error")
        )
        report["setting_writes"] = sum(t.setting_writes for t in transcripts)
        report["finished_at"] = utc_now()
        save(folder / "results.json", report)
        publish_report(folder)
        (folder / "record.md").write_text(
            "# WaveRunner 9404M performance comparison\n\n"
            f"Qualified: {report['qualified']}\n\n{report['final_state']}\n\n"
            "[Results and frame metadata](results.json) · [Summary](summary.json) · [Communication](communication.jsonl)\n\n"
            "Read frames and identifiable acquisition tokens are counted separately; no physical accuracy claim.\n",
            encoding="utf-8",
        )
        from lab_control.storage.hardware_documents import refresh_after_batch

        refresh_after_batch(folder)
    return report


def main(argv=None, *, readonly=False):
    import argparse
    from datetime import datetime

    from lab_control.application.config import load_config

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--empty-inputs", action="store_true")
    args = parser.parse_args(argv)
    configs = load_config(args.config)
    if len(configs) != 1 or (not readonly and not (args.write and args.empty_inputs)):
        parser.error("Require one exact configuration and --write --empty-inputs for the performance test")
    config = configs[0]
    name = "WAVERUNNER9404M-" + config.expected_serial
    if not config.expected_serial or any(c in name for c in '\\/:*?"<>|'):
        parser.error("Require a safe nonempty expected serial")
    folder = (
        application_home()
        / "hardware-tests"
        / name
        / (
            datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            + ("-grouped-readonly" if readonly else "-performance")
        )
    )
    folder.mkdir(parents=True, exist_ok=False)
    if readonly:
        from .grouped_queries import verify as read_only_verify

        report = read_only_verify(config, folder)
    else:
        report = verify(config, folder)
    print(folder, flush=True)
    return int(bool(report.get("error") or report.get("restoration_error")))


if __name__ == "__main__":
    raise SystemExit(main())
