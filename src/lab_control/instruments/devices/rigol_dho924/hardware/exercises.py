"""Reviewed hardware exercises; used only after a complete per-asset backup."""

from __future__ import annotations

import csv
import hashlib
import time
from typing import Any

import numpy as np

from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError, jsonable
from lab_control.transport.base import block_payload

from ..protocol import Preamble, decimal_rounding, equivalent, waveform


class Exercises:
    def __init__(self, app, device_id: str, folder, report: dict[str, Any]):
        self.app, self.device_id, self.folder, self.report = app, device_id, folder, report
        self.session = app.sessions.sessions[device_id]
        self.adapter = self.session.adapter

    def work(self, function, *args):
        return self.session.worker.submit(function, *args).result(60)

    def apply(self, operation, value=None, channel=1):
        command = DeviceCommand(operation, {} if value is None else {"value": value}, channel)
        result = self.app.sessions.submit(self.device_id, "apply", command).result(60)
        self.report.setdefault("controls", []).append(
            {"command": jsonable(command), "result": jsonable(result)}
        )
        if result.outcome != "success":
            raise DeviceError(f"{operation}: {result.error}", "control_failed")
        return result

    def qualify_word(self):
        self.apply("stop")
        preparation = self.work(self.adapter.prepare_acquisition)
        self.report["preparation"] = preparation
        enabled = tuple(ch for ch in range(1, 5) if self.work(self.adapter.read, "channel_enabled", ch))
        if not enabled:
            raise DeviceError("No enabled analog channel for stopped comparison", "unsupported")
        channel = enabled[0]

        def compare():
            transport = self.adapter.transport
            for command in (
                f":WAV:SOUR CHAN{channel}",
                ":WAV:MODE NORM",
                ":WAV:POIN 1000",
                ":WAV:STAR 1",
                ":WAV:STOP 1000",
            ):
                transport.write(command)
            pre_raw = transport.query(":WAV:PRE?")
            pre = Preamble.parse(pre_raw)
            word = transport.query_binary(":WAV:DATA?", 2000)
            transport.write(":WAV:FORM ASC")
            ascii_pre = Preamble.parse(transport.query(":WAV:PRE?"))
            raw = transport.query(":WAV:DATA?")
            (self.folder / "word-stopped.bin").write_bytes(word)
            (self.folder / "ascii-stopped.bin").write_bytes(raw)
            ascii_payload = block_payload(raw) if raw.startswith(b"#") else raw
            ascii_tokens = ascii_payload.decode("ascii").strip().strip(",").split(",")
            ascii_values = np.array([float(x) for x in ascii_tokens])
            if (
                len(ascii_values) != pre.points
                or ascii_pre.points != pre.points
                or not np.isfinite(ascii_values).all()
            ):
                raise DeviceError("ASCII point count/nonfinite values", "parse_error")
            candidates = {}
            for dtype in ("<u2", ">u2", "<i2", ">i2"):
                values = (
                    np.frombuffer(block_payload(word), dtype=dtype).astype(float)
                    - pre.y_origin
                    - pre.y_reference
                ) * pre.y_increment
                candidates[dtype] = float(np.max(np.abs(values - ascii_values)))
            measured = waveform(word, pre, channel, "V")

            # Bound the decimal rounding of the actual returned tokens. Firmware
            # prints YINC with fewer digits than ASCII samples (e.g. 6.6667E-06).
            # No tolerance based on vertical division or measured discrepancy.
            fields = pre_raw.decode("ascii").strip().split(",")
            gain_error = decimal_rounding(fields[7])
            counts = (
                np.frombuffer(block_payload(word), dtype="<u2").astype(float) - pre.y_origin - pre.y_reference
            )
            limit = np.abs(counts) * gain_error + np.array([decimal_rounding(x) for x in ascii_tokens])
            passed = bool(np.all(np.abs(np.asarray(measured.values) - ascii_values) <= limit))
            np.savez(
                self.folder / "word-ascii.npz",
                word_values=measured.values,
                ascii_values=ascii_values,
                time=measured.axis,
            )
            evidence = {
                "preamble": jsonable(pre),
                "ascii_preamble": jsonable(ascii_pre),
                "points": pre.points,
                "candidate_max_errors": candidates,
                "contract": "WORD_LE_U16",
                "passed": passed,
                "word_sha256": hashlib.sha256(word).hexdigest(),
                "tolerance": "Returned YINC and ASCII token half-last-decimal-step propagation",
                "maximum_rounding_bound": float(limit.max()),
                "raw_preamble": pre_raw.decode("ascii").strip(),
            }
            self.report["word_ascii"] = evidence
            if not passed:
                raise DeviceError("WORD/ASCII contract not qualified", "waveform_mismatch")
            transport.write(":WAV:FORM WORD")
            self.adapter.invalidate_acquisition_context("qualification")
            return self.adapter.acquire(AcquisitionRequest("waveform", channels=(channel,), confirmed=True))

        batch = self.work(compare)
        self.report["stopped_screen"] = {
            "validation": batch.validation,
            "points": [len(m.values) for m in batch.measurements],
            "metadata": [m.metadata for m in batch.measurements],
        }

    def stage(self, name):
        self.report["active_stage"] = name
        (self.folder / "progress.txt").write_text(name, encoding="utf-8")

    def completion_probe(self):
        self.stage("completion_protocol")
        t = self.adapter.transport
        rows = []
        rows.append({"step": "idle", "reply": self.work(t.query, "*OPC?").decode("ascii")})
        self.work(t.write, "*WAI")
        rows.append({"step": "after_wait", "reply": self.work(t.query, "*OPC?").decode("ascii")})
        self.work(t.write, "*OPC")
        rows.append({"step": "after_completion_marker", "reply": self.work(t.query, "*OPC?").decode("ascii")})
        rows.append({"step": "error_queue", "reply": self.work(t.query, ":SYST:ERR?").decode("ascii")})
        self.report["completion_probe"] = rows
        self.report["stages"]["completion_protocol"] = "passed"

    def controls(self):
        self.stage("parameter_write")
        self.apply("trigger_mode", "AUTO")
        self.report["parameter_cases"] = []
        for ch in (1, 2, 3, 4):
            original_enabled = self.work(self.adapter.read, "channel_enabled", ch)
            for op, target in (
                ("channel_enabled", True),
                ("scale", 0.1),
                ("offset", 0.0125),
                ("coupling", "AC"),
                ("probe_attenuation", 10),
            ):
                if op != "channel_enabled" and not self.work(self.adapter.read, "channel_enabled", ch):
                    self.apply("channel_enabled", True, ch)
                before, _ = self.work(self.adapter.snapshot)
                old = before[f"{ch}.{op}"]
                if op == "channel_enabled":
                    target = not old
                self.apply(op, target, ch)
                after, _ = self.work(self.adapter.snapshot)
                isolated = all(
                    equivalent(value, after[key])
                    for key, value in before.items()
                    if key.split(".", 1)[1]
                    in {"channel_enabled", "scale", "offset", "coupling", "probe_attenuation"}
                    and int(key.split(".", 1)[0]) != ch
                )
                if not isolated:
                    raise DeviceError("Another channel changed", "isolation_failed")
                self.apply(op, old, ch)
                restored, _ = self.work(self.adapter.snapshot)
                delta = {
                    key: {"before": value, "after": restored[key]}
                    for key, value in before.items()
                    if not equivalent(value, restored[key])
                }
                self.report["parameter_cases"].append(
                    {
                        "channel": ch,
                        "operation": op,
                        "target": target,
                        "actual": after[f"{ch}.{op}"],
                        "isolation": isolated,
                        "restoration_differences": delta,
                    }
                )
                if delta:
                    raise DeviceError("Parameter roundtrip changed dependent state", "restoration_failed")
            if not original_enabled:
                self.apply("channel_enabled", False, ch)
        for op, target in (
            ("timebase", 5e-6),
            ("timebase", 0.02),
            ("time_position", 1e-6),
            ("record_length", 1000),
            ("record_length", 100000),
            ("trigger_source", "CHAN2"),
            ("trigger_slope", "NEG"),
            ("trigger_level", 0.02),
        ):
            before, _ = self.work(self.adapter.snapshot)
            self.apply(op, target)
            actual = self.work(self.adapter.read, op)
            self.apply(op, before[f"1.{op}"])
            restored, _ = self.work(self.adapter.snapshot)
            delta = {
                key: {"before": value, "after": restored[key]}
                for key, value in before.items()
                if not equivalent(value, restored[key])
            }
            self.report["parameter_cases"].append(
                {"operation": op, "target": target, "actual": actual, "restoration_differences": delta}
            )
            if delta:
                raise DeviceError("Global parameter restoration differs", "restoration_failed")
        self.apply("trigger_mode", "NORM")
        self.apply("stop")
        # A level above empty-input noise tests arming, not acquisition completion.
        old_level = self.work(self.adapter.read, "trigger_level")
        self.apply("trigger_level", 0.1)
        self.apply("single")
        status = self.work(self.adapter.read, "trigger_status")
        self.report["single_trigger"] = {
            "status": status,
            "completed": False,
            "scope": "empty input waiting only",
        }
        if status != "WAIT":
            raise DeviceError("Single trigger did not remain waiting", "state_mismatch")
        self.apply("stop")
        self.apply("trigger_level", old_level)
        self.apply("trigger_mode", "AUTO")
        self.report["stages"]["parameter_write"] = "passed"

    def restore_initial(self, initial):
        """Single restoration pass, only from a fresh readable state and backup."""
        self.stage("restoration")
        current, _ = self.work(self.adapter.snapshot)
        vertical = ("probe_attenuation", "scale", "offset", "coupling")
        for ch in range(1, 5):
            if not current[f"{ch}.channel_enabled"] and any(
                not equivalent(current[f"{ch}.{op}"], initial["values"][f"{ch}.{op}"]) for op in vertical
            ):
                result = self.work(self.adapter.apply, DeviceCommand("channel_enabled", {"value": True}, ch))
                if result.outcome != "success":
                    raise DeviceError("Cannot enable channel for restoration", "restoration_failed")
        order = [(ch, op) for op in (*vertical, "channel_enabled") for ch in range(1, 5)]
        order += [
            (1, op)
            for op in (
                "record_length",
                "timebase",
                "time_position",
                "trigger_source",
                "trigger_slope",
                "trigger_level",
            )
        ]
        for ch, op in order:
            value = initial["values"][f"{ch}.{op}"]
            actual = self.work(self.adapter.read, op, ch)
            if equivalent(value, actual):
                continue
            result = self.work(self.adapter.apply, DeviceCommand(op, {"value": value}, ch))
            self.report.setdefault("restore_commands", []).append(
                {"operation": op, "channel": ch, "result": jsonable(result)}
            )
            if result.outcome != "success":
                raise DeviceError("Restoration failed: " + op + ": " + result.error, "restoration_failed")
        self.work(self.adapter.restore_transfer, initial["transfer"])
        # Restore sweep even when the original acquisition was stopped.
        sweep = initial["values"]["1.trigger_sweep"]
        if self.work(self.adapter.read, "trigger_sweep") != sweep:
            self.work(self.adapter.transport.write, ":TRIG:SWE " + sweep)
        mode = initial["trigger_mode"]
        if self.work(self.adapter.read, "trigger_mode") != mode:
            result = self.work(self.adapter.apply, DeviceCommand("trigger_mode", {"value": mode}))
            if result.outcome != "success":
                raise DeviceError(
                    "Acquisition mode restoration failed: " + result.error, "restoration_failed"
                )

    def waveforms(self):
        self.stage("waveform")
        for ch in range(1, 5):
            self.apply("channel_enabled", True, ch)
        self.apply("record_length", 10000)
        self.apply("timebase", 2e-6)
        # Positive offset makes codes cross 32767, distinguishing signed from
        # unsigned WORD on the same stopped ASCII comparison.
        self.apply("offset", 0.025, 1)
        self.apply("trigger_mode", "AUTO")
        time.sleep(0.5)
        self.qualify_word()
        if self.report["word_ascii"]["candidate_max_errors"]["<i2"] < 0.1:
            raise DeviceError("Sign qualification lacks high-bit samples", "waveform_mismatch")
        request = AcquisitionRequest("waveform", (1, 2, 3, 4), confirmed=True)
        screen = self.app.sessions.submit(self.device_id, "acquire", request).result(60)
        memory = self.app.monitoring.read_memory_waveform(self.device_id, request).result(60)
        self.report["waveforms"] = {
            "screen": [m.metadata for m in screen.measurements],
            "memory": [m.metadata for m in memory.batch.measurements],
        }
        self.report["single_save"] = str(
            self.app.monitoring.export_displayed_frame(self.device_id, memory, "electronic").result(60)
        )
        self.report["stages"]["waveform"] = "passed"
        self.report["stages"]["single_save"] = "passed"

    def monitoring(self, duration=60):
        self.apply("trigger_mode", "AUTO")
        request = AcquisitionRequest("waveform", (1, 2, 3, 4), confirmed=True)
        service = self.app.monitoring
        service.start(self.device_id, (request,), interval=0.5, record_interval=None, cadence="interval")
        task = service.tasks[self.device_id]
        self.stage("monitoring")
        deadline = time.monotonic() + duration
        adjusted = False
        memory_read = False
        while time.monotonic() < deadline:
            if not task.running:
                raise DeviceError("Monitoring paused: " + task.error, "monitoring_failed")
            if task.sequence >= 2 and not adjusted:
                service.sessions.set_control_group(self.device_id, True)
                try:
                    self.apply("offset", 0.02, 1)
                    self.apply("offset", 0.025, 1)
                finally:
                    service.sessions.set_control_group(self.device_id, False)
                adjusted = True
            if task.sequence >= 4 and not memory_read:
                frame = service.read_memory_waveform(self.device_id, request).result(60)
                self.report["memory_during_monitoring"] = {
                    "points": [len(m.values) for m in frame.batch.measurements],
                    "running": task.running,
                    "mode": self.work(self.adapter.read, "trigger_mode"),
                }
                memory_read = True
            time.sleep(0.05)
        if not adjusted or not memory_read or task.sequence < 6:
            raise DeviceError("Insufficient valid monitoring frames", "monitoring_failed")
        self.report["monitoring"] = {
            "frames": task.sequence,
            "timings": list(task.timings),
            "seconds": duration,
            "continuous_controls": adjusted,
        }
        self.report["stages"]["monitoring"] = "passed"
        self.stage("recording")
        service.set_recording(self.device_id, True)
        try:
            self.app.sessions.submit(self.device_id, "apply", DeviceCommand("offset", {"value": 0.01}))
        except DeviceError:
            self.report["recording_lock_rejected"] = True
        else:
            raise DeviceError("Recording failed to lock parameters", "lock_failed")
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if not task.running:
                raise DeviceError("Recording paused: " + task.error, "recording_failed")
            time.sleep(0.05)
        service.stop(self.device_id).result(60)
        recorder = task.recorder
        if recorder is None:
            raise DeviceError("No recorder", "recording_failed")
        recorder.finished.result(60)
        status = recorder.snapshot()
        self.report["recording"] = {**status, "folder": str(recorder.folder), "timings": list(task.timings)}
        if (
            not status["binary_done"]
            or status.get("error")
            or status.get("csv_error")
            or status["received"] != status["committed"]
            or status["converted"] != status["committed"]
            or not status["committed"]
        ):
            raise DeviceError("Recording did not drain and convert", "recording_failed")
        self.report["binary_csv"] = verify_saved_samples(recorder.folder)
        self.apply("stop")
        self.report["stages"]["recording"] = "passed"


def verify_saved_samples(folder):
    from lab_control.storage.recording import read_frames

    frames = samples = 0
    for path in sorted(folder.glob("segment-*.sqlite3")):
        with path.with_suffix(".csv").open(encoding="utf-8", newline="") as stream:
            rows = csv.DictReader(stream)
            for frame in read_frames(path):
                frames += 1
                for m in frame["measurements"]:
                    for x, value in zip(m["axis"], m["values"], strict=True):
                        row = next(rows)
                        if (
                            int(row["sequence"]) != frame["sequence"]
                            or int(row["channel"]) != m["channel"]
                            or float(row["axis"]) != x
                            or float(row["real"]) != value.real
                            or float(row["imaginary"]) != value.imag
                        ):
                            raise DeviceError("CSV differs from binary samples", "recording_failed")
                        samples += 1
            if next(rows, None) is not None:
                raise DeviceError("Extra CSV samples", "recording_failed")
    if not frames or not samples:
        raise DeviceError("Empty recording", "recording_failed")
    return {"frames": frames, "samples": samples, "equal": True}
