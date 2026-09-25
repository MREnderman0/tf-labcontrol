"""Opt-in, output-OFF AFG31252 USB/LAN acceptance; no nonvolatile storage writes.

Run with the project Python and --config <runtime TOML>. --preflight-only backs
up and checks the connected asset without changing any instrument settings.
Every invocation creates its own asset/time directory, including failed runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import re
import subprocess
import time
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pyvisa

from lab_control.application.config import ConnectionConfig, load_config
from lab_control.application.runtime import ApplicationRuntime
from lab_control.discovery.service import ConfiguredDiscoveryProvider
from lab_control.domain.instruments import DeviceCommand, jsonable, utc_now
from lab_control.instruments.common.hardware_acceptance import (
    enumerate_configured,
    interface_label,
    visa_interface,
)
from lab_control.instruments.devices.tek_afg31252.adapter import Afg31252Adapter
from lab_control.instruments.registry import create_adapter
from lab_control.storage.repository import application_home

CONNECTION_NOTE = "本批仅验证所选接口；其他接口及历史排查结果见资产结构化测试说明。"
SOURCE_PATHS = (
    "FUNC",
    "FREQ",
    "VOLT",
    "VOLT:UNIT",
    "VOLT:OFFS",
    "VOLT:LIM:HIGH",
    "VOLT:LIM:LOW",
    "COMB:FEED",
    "PHAS",
    "PULS:HOLD",
    "PULS:PER",
    "PULS:DCYC",
    "PULS:WIDT",
    "PULS:DEL",
    "PULS:TRAN:LEAD",
    "PULS:TRAN:TRA",
    "FUNC:RAMP:SYMM",
    "FREQ:MODE",
    "FREQ:CONC",
    "VOLT:CONC",
    "PHAS:CONC",
    "FREQ:STAR",
    "FREQ:STOP",
    "SWE:TIME",
    "SWE:SPAC",
    "SWE:HTIM",
    "SWE:RTIM",
    "SWE:MODE",
    "AM:STAT",
    "AM:SOUR",
    "AM:INT:FREQ",
    "AM:INT:FUNC",
    "AM:DEPT",
    "FM:STAT",
    "FM:SOUR",
    "FM:INT:FREQ",
    "FM:INT:FUNC",
    "FM:DEV",
    "PM:STAT",
    "PM:SOUR",
    "PM:INT:FREQ",
    "PM:INT:FUNC",
    "PM:DEV",
    "BURS:STAT",
    "BURS:MODE",
    "BURS:NCYC",
    "BURS:TDEL",
    "BURS:IDLE",
    "FSK:STAT",
    "PWM:STAT",
    "RMOD",
)
GLOBAL_PATHS = ("TRIG:SOUR", "TRIG:TIM", "TRIG:SLOP", "OUTP:TRIG:MODE", "SOUR:ROSC:SOUR")
QUERIES = tuple(
    [path + "?" for path in GLOBAL_PATHS]
    + [f"SOUR{ch}:{path}?" for ch in (1, 2) for path in SOURCE_PATHS]
    + [f"OUTP{ch}{suffix}?" for ch in (1, 2) for suffix in ("", ":IMP", ":POL")]
)
READ_ONLY_PATHS = {"RMOD", "VOLT:LIM:HIGH", "VOLT:LIM:LOW", "COMB:FEED"}


def error_text(exc: Exception) -> str:
    return str(exc) or type(exc).__name__


def validate_snapshot(values: dict[str, str]) -> None:
    """Reject malformed/merged state responses before authorizing any setting writes."""
    for ch in (1, 2):
        choices = {
            f"OUTP{ch}?": {"0", "1"},
            f"OUTP{ch}:POL?": {"NORM", "INV"},
            f"SOUR{ch}:FUNC?": {"SIN", "SQU", "RAMP", "PULS", "PRN", "DC", "EMEM"},
            f"SOUR{ch}:FREQ:MODE?": {"CW", "SWE"},
        }
        for query, allowed in choices.items():
            if values[query] not in allowed:
                raise RuntimeError(f"Invalid snapshot response: {query} -> {values[query]!r}")


def same_value(expected: Any, actual: Any) -> bool:
    try:
        return math.isclose(float(expected), float(actual), rel_tol=1e-5, abs_tol=1e-9)
    except (ValueError, TypeError):
        return str(expected).strip().upper() == str(actual).strip().upper()


def differences(expected: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    return {
        q: {"expected": value, "actual": actual.get(q)}
        for q, value in expected.items()
        if q not in actual or not same_value(value, actual[q])
    }


def edit_selection_matches(raw: str, channel: int) -> bool:
    # 077148803 p119 defines the return as EMEM, without a memory number.
    # This verifies selection of an edit waveform, not its physical output.
    return raw.strip().upper() in {"EMEM", f"EMEM{channel}"}


def replace_report(temporary: Path, destination: Path) -> None:
    # Windows readers may briefly deny replacement. Retry the local rename
    # only; this function never retries an instrument command.
    for attempt in range(20):
        try:
            temporary.replace(destination)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


def checked_restore(actions: list[tuple[str, Callable[[], Any]]]) -> list[dict[str, str]]:
    """Stop an ordered restore at the first failure; never report a partial restore as success."""
    results = []
    for name, action in actions:
        try:
            action()
            results.append({"step": name, "status": "passed"})
        except Exception as exc:
            results.append({"step": name, "status": "failed", "error": str(exc)})
            break
    return results


class GuardedTransport:
    def __init__(self, inner, folder: Path, config: ConnectionConfig):
        self.inner, self.folder, self.config = inner, folder, config
        self.resource = inner.resource
        self.allow_settings = False
        self.write_count = 0
        self.sequence = 0

    def open(self) -> None:
        raise RuntimeError("The session owns opening this transport")

    def log(self, direction: str, payload: bytes | str) -> None:
        self.sequence += 1
        record: dict[str, Any] = {"time": utc_now(), "direction": direction}
        if isinstance(payload, bytes) and (payload.startswith(b"#") or b",#" in payload):
            name = f"{id(self):x}-{self.sequence:06d}-{direction}.bin"
            (self.folder / name).write_bytes(payload)
            record.update(file=name, bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        else:
            record["data"] = (
                payload.decode("ascii", errors="backslashreplace") if isinstance(payload, bytes) else payload
            )
        with (self.folder / "communication.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def write(self, payload: bytes | str) -> None:
        if self.resource != self.config.resource:
            raise RuntimeError("Unexpected hardware resource")
        text = payload.decode("ascii", errors="replace") if isinstance(payload, bytes) else payload
        query = text in (*QUERIES, "*IDN?", "*OPC?", "SYST:ERR?", "DATA:DATA? EMEM1", "DATA:DATA? EMEM2")
        if not query:
            if not self.allow_settings:
                raise RuntimeError("Settings forbidden until all backups are complete")
            if text.startswith("OUTP") and text not in ("OUTP1 OFF", "OUTP2 OFF"):
                raise RuntimeError("Output enabling and output connector changes are forbidden")
            binary = re.match(rb"^DATA:DATA EMEM[12],#", payload) if isinstance(payload, bytes) else None
            writable = {
                q[:-1]
                for q in QUERIES
                if not q.endswith((":RMOD?", ":VOLT:LIM:HIGH?", ":VOLT:LIM:LOW?", ":COMB:FEED?"))
            }
            if not binary and (";" in text or text.split(" ", 1)[0] not in writable):
                raise RuntimeError("Command outside the acceptance test allowlist")
            self.write_count += 1
        self.log("tx", payload)
        self.inner.write(payload)

    def read(self) -> bytes:
        try:
            result = self.inner.read()
        except Exception as exc:
            self.log("error", error_text(exc))
            raise
        self.log("rx", result)
        return result

    def query(self, payload: bytes | str) -> bytes:
        self.write(payload)
        return self.read()

    def close(self) -> None:
        self.inner.close()


class Acceptance:
    def __init__(
        self,
        config: ConnectionConfig,
        folder: Path,
        preflight_only: bool = False,
        restore_from: Path | None = None,
    ):
        self.config, self.folder, self.preflight_only = config, folder, preflight_only
        self.restore_from = restore_from
        self.app: ApplicationRuntime | None = None
        self.device_id = ""
        self.guards: list[GuardedTransport] = []
        self.baseline: dict[str, str] = {}
        self.memories: dict[int, bytes] = {}
        self.memory_written: set[int] = set()
        self.current_group = "preflight"
        self.report: dict[str, Any] = {
            "started_at": utc_now(),
            "scope": f"{interface_label(config)} controls with both outputs OFF; no physical measurement",
            "resource": config.resource,
            "expected_serial": config.expected_serial,
            "connection_note": CONNECTION_NOTE,
            "environment": {"python": platform.python_version(), "pyvisa": pyvisa.__version__},
            "stages": {
                name: "not_tested"
                for name in (
                    "scan",
                    "identity",
                    "connection",
                    "backup",
                    "basic",
                    "pulse_ramp",
                    "sweep",
                    "am",
                    "fm",
                    "pm",
                    "burst",
                    "arbitrary",
                    "repeat_control",
                    "restoration",
                    "repeat_read",
                    "reconnection",
                    "physical_output",
                )
            },
            "operations": [],
            "restore_steps": [],
            "reconnections": [],
        }

    def save(self) -> None:
        path = self.folder / "results.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(jsonable(self.report), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        replace_report(temporary, path)
        for name in ("initial_snapshot", "final_snapshot", "restore_target_snapshot"):
            if name in self.report:
                (self.folder / f"{name}.json").write_text(
                    json.dumps(self.report[name], ensure_ascii=False, indent=2), encoding="utf-8"
                )
        from lab_control.instruments.devices.tek_afg31252.hardware.records import publish_report

        publish_report(self.folder)

    def worker(self, fn):
        assert self.app is not None
        session = self.app.sessions.sessions[self.device_id]
        return session.worker.submit(lambda: fn(session.adapter)).result(timeout=120)

    def snapshot(self, channel: int | None = None) -> dict[str, str]:
        queries = (
            QUERIES
            if channel is None
            else tuple(q for q in QUERIES if q.startswith((f"SOUR{channel}:", f"OUTP{channel}")))
        )

        def read(adapter):
            values = {}
            for query in queries:
                values[query] = adapter.transport.query(query).decode("ascii").strip()
            return values

        return self.worker(read)

    def outputs_off(self) -> None:
        values = self.worker(lambda a: [a.read("output", ch) for ch in (1, 2)])
        if any(float(value) != 0 for value in values):
            raise RuntimeError(f"Output OFF invariant failed: {values}")

    def errors(self) -> list[str]:
        def read(adapter):
            errors: list[str] = []
            for _ in range(32):
                value = adapter.transport.query("SYST:ERR?").decode("ascii").strip()
                if value.split(",", 1)[0].strip() in {"0", "+0"}:
                    return errors
                errors.append(value)
            raise RuntimeError("Error queue did not terminate within 32 reads")

        return self.worker(read)

    def apply(self, channel: int, operation: str, value: Any = None, **parameters) -> None:
        assert self.app is not None
        self.outputs_off()
        before = self.snapshot(3 - channel)
        args = parameters or ({} if value is None else {"value": value})
        entry = {
            "time": utc_now(),
            "group": self.current_group,
            "channel": channel,
            "operation": operation,
            "parameters": args,
        }
        self.report["operations"].append(entry)
        self.save()
        try:
            outcome = self.app.sessions.submit(
                self.device_id, "apply", DeviceCommand(operation, args, channel, confirmed=True)
            ).result(timeout=120)
        except Exception as exc:
            entry["error"] = str(exc)
            self.save()
            raise
        entry["result"] = jsonable(outcome)
        self.save()
        if outcome.outcome != "success":
            raise RuntimeError(f"{operation}: {outcome.outcome}: {outcome.error}")
        self.outputs_off()
        entry["other_channel_differences"] = differences(before, self.snapshot(3 - channel))
        entry["both_outputs_off"] = True
        if entry["other_channel_differences"]:
            raise RuntimeError(f"Cross-channel change after {channel}.{operation}")
        self.save()

    def raw_set(self, query: str, value: str) -> None:
        if (
            query not in QUERIES
            or any(query.endswith(f":{path}?") for path in READ_ONLY_PATHS)
            or query.startswith("OUTP")
        ):
            raise RuntimeError("Unsupported restoration/setup field")

        def setting(adapter):
            before = adapter.transport.query(query).decode("ascii").strip()
            if same_value(value, before):
                return
            adapter.transport.write(query[:-1] + " " + value)
            actual = adapter.transport.query(query).decode("ascii").strip()
            error = adapter.transport.query("SYST:ERR?").decode("ascii").strip()
            if error.split(",", 1)[0].strip() not in {"0", "+0"} or not same_value(value, actual):
                raise RuntimeError(f"{query}: requested {value}, read {actual}, error {error}")

        self.worker(setting)
        self.outputs_off()

    def quiet(self, channel: int) -> None:
        for operation in ("am_enabled", "fm_enabled", "pm_enabled", "burst"):
            self.apply(channel, operation, False)
        self.apply(channel, "frequency_mode", "CW")

    def group(self, name: str, action) -> None:
        self.current_group = name
        self.report["stages"][name] = "running"
        self.save()
        action()
        errors = self.errors()
        if errors:
            raise RuntimeError(f"Errors after {name}: {errors}")
        self.report["stages"][name] = "passed"
        self.save()
        print(f"{name}: passed", flush=True)

    def basic(self) -> None:
        for ch in (1, 2):
            self.quiet(ch)
            self.apply(ch, "waveform", "SIN")
            self.apply(ch, "amplitude_unit", "VPP")
            self.apply(ch, "offset", 0)
            for freq, amp, offset, phase in ((1000, 0.1, -0.05, 0), (2000, 0.2, 0.05, 0.5)):
                for operation, value in (
                    ("amplitude", amp),
                    ("frequency", freq),
                    ("offset", offset),
                    ("phase", phase),
                ):
                    self.apply(ch, operation, value)
            for waveform in ("SQU", "RAMP", "PULS", "NOIS", "DC", "SIN"):
                self.apply(ch, "waveform", waveform)
                if waveform not in {"NOIS", "DC"}:
                    for frequency in (1000, 2000):
                        self.apply(ch, "frequency", frequency)
            self.apply(ch, "offset", 0)

    def pulse_ramp(self) -> None:
        for ch in (1, 2):
            self.apply(ch, "waveform", "PULS")
            self.apply(ch, "frequency", 1000)
            for duty in (25, 75):
                self.apply(ch, "pulse_duty", duty)
                width = self.worker(lambda a: a.read("pulse_width", ch))
                self.report["operations"][-1]["related_pulse_width"] = width
                if not same_value(width, duty / 100 / 1000):
                    raise RuntimeError("Pulse width/duty relationship mismatch")
            self.apply(ch, "pulse_width", 0.00025)
            duty = self.worker(lambda a: a.read("pulse_duty", ch))
            self.report["operations"][-1]["related_pulse_duty"] = duty
            if not same_value(duty, 25):
                raise RuntimeError("Pulse duty/width relationship mismatch")
            self.apply(ch, "waveform", "RAMP")
            for symmetry in (25, 75):
                self.apply(ch, "ramp_symmetry", symmetry)
            self.apply(ch, "waveform", "SIN")

    def sweep(self) -> None:
        for ch in (1, 2):
            self.quiet(ch)
            # Expand first so either initial endpoint order can be restored safely later.
            stop = self.worker(lambda a: a.read("sweep_stop", ch))
            if float(stop) < 1000:
                self.apply(ch, "sweep_stop", 2000)
            self.apply(ch, "sweep_start", 1000)
            self.apply(ch, "sweep_stop", 2000)
            for spacing, duration in (("LIN", 1), ("LOG", 2)):
                self.apply(ch, "sweep_spacing", spacing)
                self.apply(ch, "sweep_time", duration)
                self.apply(ch, "frequency_mode", "SWE")
                self.apply(ch, "frequency_mode", "CW")

    def modulation(self, kind: str) -> None:
        settings = {
            "am": ("am_depth", (10, 20)),
            "fm": ("fm_deviation", (100, 200)),
            "pm": ("pm_deviation", (10, 20)),
        }
        for ch in (1, 2):
            self.quiet(ch)
            for operation, value in (("waveform", "SIN"), ("frequency", 10000), ("amplitude", 0.2)):
                self.apply(ch, operation, value)
            self.apply(ch, kind + "_source", "INT")
            self.apply(ch, kind + "_frequency", 100)
            operation, values = settings[kind]
            for value in values:
                self.apply(ch, operation, value)
            self.apply(ch, kind + "_enabled", True)
            self.apply(ch, kind + "_enabled", False)

    def burst(self) -> None:
        self.raw_set("TRIG:SOUR?", "EXT")
        for ch in (1, 2):
            self.quiet(ch)
            for mode in ("TRIG", "GAT"):
                self.apply(ch, "burst_mode", mode)
                if mode == "TRIG":
                    for count in (1, 5):
                        self.apply(ch, "burst_cycles", count)
                self.apply(ch, "burst", True)
                self.apply(ch, "burst", False)

    def arbitrary(self) -> None:
        for ch in (1, 2):
            samples = np.sin(2 * np.pi * np.arange(256) / 256) if ch == 1 else np.linspace(-1, 1, 256)
            other = self.worker(lambda a: a.read_edit_memory(3 - ch))
            self.memory_written.add(ch)  # Mark before sending: a timeout may follow a completed write.
            self.apply(ch, "upload_waveform", samples=samples.tolist())
            actual = self.worker(lambda a: a.read_edit_memory(ch))
            expected = np.rint((samples + 1) * 8191).astype(">u2").tobytes()
            if actual != expected or self.worker(lambda a: a.read_edit_memory(3 - ch)) != other:
                raise RuntimeError("Arbitrary data encoding or memory isolation mismatch")
            self.report.setdefault("arbitrary_data", {})[str(ch)] = {
                "points": 256,
                "sha256": hashlib.sha256(actual).hexdigest(),
                "exact_match": True,
            }
            self.apply(ch, "select_uploaded_waveform")
            selected = str(self.worker(lambda a: a.read("waveform", ch))).upper()
            self.report["arbitrary_data"][str(ch)].update(
                selected_readback=selected,
                requested_memory=f"EMEM{ch}",
                selection_evidence="Explicit memory command accepted; FUNC? reports edit-waveform type only",
            )
            if not edit_selection_matches(selected, ch):
                raise RuntimeError(f"Unexpected selected edit memory: {selected}")
            self.apply(ch, "waveform", "SIN")

    def repeat_control(self) -> None:
        for index in range(10):
            for ch in (1, 2):
                self.apply(ch, "frequency", 1000 if index % 2 == 0 else 2000)
                self.apply(ch, "amplitude", 0.1 if index % 2 == 0 else 0.2)

    def restore(self) -> None:
        assert self.app is not None
        self.current_group = "restoration"
        # Read actual state first. If a query fails, do not write through an uncertain session.
        self.report["before_restore"] = self.snapshot()
        self.report["errors_before_restore"] = self.errors()
        if self.app.sessions.sessions[self.device_id].state == "degraded":
            self.app.sessions.disconnect(self.device_id)
            self.app.connect(self.device_id, configure_acquisition=False).result(timeout=30)
        for guard in self.guards:
            guard.allow_settings = True
        actions: list[tuple[str, Callable[[], Any]]] = []
        for ch in (1, 2):
            actions.append((f"CH{ch} output OFF", partial(self.apply, ch, "output", False)))
            actions.append((f"CH{ch} quiet", partial(self.quiet, ch)))

        def restore_memory(ch: int) -> None:
            self.worker(lambda a: a.restore_edit_memory(ch, self.memories[ch]))

        for ch in sorted(self.memory_written):
            actions.append((f"EMEM{ch} restore", partial(restore_memory, ch)))
        for ch in (1, 2):

            def restore_channel(ch: int) -> None:
                prefix = f"SOUR{ch}:"
                self.apply(ch, "waveform", "PULS")
                self.raw_set(prefix + "PULS:DEL?", "0")
                for path in (
                    "PULS:TRAN:LEAD",
                    "PULS:TRAN:TRA",
                    "PULS:WIDT",
                    "PULS:PER",
                    "PULS:DCYC",
                    "PULS:WIDT",
                    "PULS:DEL",
                    "PULS:HOLD",
                ):
                    self.raw_set(prefix + path + "?", self.baseline[prefix + path + "?"])
                self.apply(ch, "waveform", "SIN")
                self.apply(ch, "offset", 0)
                self.apply(ch, "amplitude", 0.1)
                self.raw_set(prefix + "FREQ?", self.baseline[prefix + "FREQ?"])
                start = float(self.baseline[prefix + "FREQ:STAR?"])
                current_stop = float(self.worker(lambda a: a.read("sweep_stop", ch)))
                order = ("FREQ:STOP", "FREQ:STAR") if start > current_stop else ("FREQ:STAR", "FREQ:STOP")
                for path in order:
                    self.raw_set(prefix + path + "?", self.baseline[prefix + path + "?"])
                excluded = {
                    "FUNC",
                    "FREQ",
                    "FREQ:STAR",
                    "FREQ:STOP",
                    "VOLT",
                    "VOLT:OFFS",
                    "VOLT:UNIT",
                    "RMOD",
                } | READ_ONLY_PATHS
                for path in SOURCE_PATHS:
                    if (
                        path in excluded
                        or path.startswith("PULS:")
                        or path.endswith((":STAT", ":CONC"))
                        or path == "FREQ:MODE"
                    ):
                        continue
                    self.raw_set(prefix + path + "?", self.baseline[prefix + path + "?"])
                for path in ("VOLT:UNIT", "VOLT", "VOLT:OFFS", "FUNC"):
                    self.raw_set(prefix + path + "?", self.baseline[prefix + path + "?"])
                for path in ("FREQ:MODE", "AM:STAT", "FM:STAT", "PM:STAT", "BURS:STAT"):
                    value = self.baseline[prefix + path + "?"]
                    # OFF commands can switch modes; activate the original mode only after all OFFs.
                    if value not in ("0", "CW"):
                        self.raw_set(prefix + path + "?", value)

            actions.append((f"CH{ch} parameters and mode", partial(restore_channel, ch)))
        actions.append(("trigger source", lambda: self.raw_set("TRIG:SOUR?", self.baseline["TRIG:SOUR?"])))
        for ch in (1, 2):
            for path in ("FREQ:CONC", "VOLT:CONC", "PHAS:CONC"):
                query = f"SOUR{ch}:{path}?"
                actions.append((query, partial(self.raw_set, query, self.baseline[query])))
        self.report["restore_steps"] = checked_restore(actions)
        self.save()
        final = self.snapshot()
        self.report["final_snapshot"] = final
        expected = {**self.baseline, "OUTP1?": "0", "OUTP2?": "0"}
        self.report["restoration_differences"] = differences(expected, final)
        hashes = {}
        for ch in (1, 2):
            data = self.worker(lambda a: a.read_edit_memory(ch))
            hashes[str(ch)] = {
                "sha256": hashlib.sha256(data).hexdigest(),
                "matches_original": data == self.memories[ch],
            }
        self.report["final_memory"] = hashes
        okay = (
            not self.report["restoration_differences"]
            and all(x["status"] == "passed" for x in self.report["restore_steps"])
            and all(x["matches_original"] for x in hashes.values())
        )
        self.report["stages"]["restoration"] = "passed" if okay else "failed"
        self.save()
        if not okay:
            raise RuntimeError("Restoration is incomplete; see actual final state and restore_steps")

    def run(self) -> None:
        try:
            # Per-batch audit database; ApplicationRuntime still takes the fixed global hardware lock.
            self.app = ApplicationRuntime((self.config,), root=self.folder / "runtime")
            self.report["environment"]["audit_database"] = str(self.folder / "runtime/inventory.sqlite3")

            def factory(config):
                assert self.app is not None
                adapter = create_adapter(config, self.app.visa)
                if not isinstance(adapter, Afg31252Adapter):
                    adapter.close()
                    raise RuntimeError("Exact AFG adapter required")
                guard = GuardedTransport(adapter.transport, self.folder, self.config)
                adapter.transport = guard
                self.guards.append(guard)
                return adapter

            self.app.sessions.factory = factory

            def probe(candidate):
                adapter = factory(candidate.config)
                try:
                    return adapter.identify(), candidate.config.adapter
                finally:
                    adapter.close()

            self.app.discovery.probe = probe
            self.app.discovery.providers = (ConfiguredDiscoveryProvider((self.config,)),)
            self.report["enumeration"] = list(enumerate_configured(self.app.visa, self.config))
            self.report["stages"]["enumeration"] = (
                "passed"
                if self.config.resource.upper() in {r.upper() for r in self.report["enumeration"]}
                else "blocked"
            )
            self.report["scan"] = self.app.discovery.scan().result(timeout=30)
            if any(item.get("error") for item in self.report["scan"]["items"]):
                raise RuntimeError("Identity scan failed")
            self.report["stages"]["scan"] = "passed"
            self.device_id = self.app.add(self.config)
            self.report["identity"] = jsonable(
                self.app.connect(self.device_id, configure_acquisition=False).result(timeout=30)
            )
            self.report["stages"].update(identity="passed", connection="passed")
            self.report["environment"]["visa"] = str(self.app.visa.manager.visalib)
            self.report["initial_errors"] = self.errors()
            self.current_group = "backup"
            self.baseline = self.snapshot()
            self.report["initial_snapshot"] = self.baseline
            self.save()
            validate_snapshot(self.baseline)
            for ch in (1, 2):
                self.memories[ch] = self.worker(lambda a: a.read_edit_memory(ch))
                (self.folder / f"EMEM{ch}-original.bin").write_bytes(self.memories[ch])
            self.report["memory_backup"] = {
                str(ch): {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for ch, data in self.memories.items()
            }
            if self.errors():
                raise RuntimeError("Backup queries produced instrument errors")
            for ch in (1, 2):
                if self.restore_from:
                    continue  # Recovery uses the earlier verified target, not this temporary state.
                if any(float(self.baseline[f"SOUR{ch}:{path}:STAT?"]) != 0 for path in ("FSK", "PWM")):
                    raise RuntimeError("Active FSK/PWM is outside this test's restoration scope")
                if self.baseline[f"SOUR{ch}:FUNC?"] not in {"SIN", "SQU", "RAMP", "PULS", "PRN", "DC"}:
                    raise RuntimeError(
                        "Initial waveform cannot be unambiguously restored (EMEM query omits bank)"
                    )
                if self.baseline[f"SOUR{ch}:RMOD?"] not in {"CONT", "MOD", "SWE", "BURS"}:
                    raise RuntimeError("Initial run mode is outside restoration scope")
            self.report["stages"]["backup"] = "passed"
            self.save()
            if self.preflight_only:
                return
            if self.restore_from:
                previous = json.loads((self.restore_from / "results.json").read_text(encoding="utf-8"))
                if (
                    previous.get("expected_serial") != self.config.expected_serial
                    or previous.get("resource") != self.config.resource
                ):
                    raise RuntimeError("Recovery backup belongs to another asset")
                self.baseline = previous["initial_snapshot"]
                self.report["restore_target_snapshot"] = self.baseline
                self.report["restore_from"] = str(self.restore_from)
                for ch in (1, 2):
                    original = (self.restore_from / f"EMEM{ch}-original.bin").read_bytes()
                    if hashlib.sha256(original).hexdigest() != previous["memory_backup"][str(ch)]["sha256"]:
                        raise RuntimeError("Recovery memory backup hash mismatch")
                    if original != self.memories[ch]:
                        self.memory_written.add(ch)
                    self.memories[ch] = original
                for guard in self.guards:
                    guard.allow_settings = True
                self.restore()
                return
            self.outputs_off()
            for guard in self.guards:
                guard.allow_settings = True
            try:
                for ch in (1, 2):
                    for path in ("FREQ:CONC", "VOLT:CONC", "PHAS:CONC"):
                        self.raw_set(f"SOUR{ch}:{path}?", "0")
                for name, action in (
                    ("basic", self.basic),
                    ("pulse_ramp", self.pulse_ramp),
                    ("sweep", self.sweep),
                    ("am", lambda: self.modulation("am")),
                    ("fm", lambda: self.modulation("fm")),
                    ("pm", lambda: self.modulation("pm")),
                    ("burst", self.burst),
                    ("arbitrary", self.arbitrary),
                    ("repeat_control", self.repeat_control),
                ):
                    self.group(name, action)
            except Exception as exc:
                self.report["stages"][self.current_group] = "failed"
                self.report["control_error"] = error_text(exc)
                print(f"control stopped: {exc}", flush=True)
            finally:
                self.restore()
            self.current_group = "repeat_read"
            expected = self.report["final_snapshot"]
            readings: list[dict[str, Any]] = []
            self.report["repeated_reads"] = readings
            for index in range(60):
                status = jsonable(self.app.sessions.submit(self.device_id, "status").result(timeout=30))
                readings.append(status)
                values = status["values"]
                for ch in (1, 2):
                    for name, path in (("frequency", "FREQ"), ("amplitude", "VOLT"), ("offset", "VOLT:OFFS")):
                        if not same_value(values[f"{ch}.{name}"], expected[f"SOUR{ch}:{path}?"]):
                            raise RuntimeError("Repeated read changed")
                self.outputs_off()
                self.save()
                if index != 59:
                    time.sleep(1)
            self.report["stages"]["repeat_read"] = "passed"
            self.current_group = "reconnection"
            for attempt in range(3):
                self.app.sessions.disconnect(self.device_id)
                identity = jsonable(
                    self.app.connect(self.device_id, configure_acquisition=False).result(timeout=30)
                )
                snapshot = self.snapshot()
                diff = differences(expected, snapshot)
                self.report["reconnections"].append(
                    {"attempt": attempt + 1, "identity": identity, "differences": diff}
                )
                if diff or identity != self.report["identity"]:
                    raise RuntimeError("Reconnection identity/state changed")
            self.report["stages"]["reconnection"] = "passed"
        except Exception as exc:
            self.report["error"] = error_text(exc)
            self.report["error_type"] = type(exc).__name__
            if self.report["stages"].get(self.current_group) in {"running", "not_tested"}:
                self.report["stages"][self.current_group] = "failed"
            if self.report["stages"].get("restoration") == "not_tested" and any(
                g.write_count for g in self.guards
            ):
                self.report["stages"]["restoration"] = "failed"
                self.report["final_state"] = "Not fully verified; see transcript and restore steps"
            print(f"run stopped: {exc}", flush=True)
        finally:
            if self.app:
                try:
                    self.app.close()
                    self.report["session_released"] = True
                except Exception as exc:
                    self.report["close_error"] = str(exc)
            self.report["finished_at"] = utc_now()
            self.report["setting_write_count"] = sum(g.write_count for g in self.guards)
            if not self.report["setting_write_count"]:
                self.report["final_state"] = (
                    "No instrument settings written; last observed state only, see snapshot/transcript"
                )
            self.save()
            self.write_record()

    def write_record(self) -> None:
        stages = self.report["stages"]
        rows = "\n".join(f"| {name} | {state} |" for name, state in stages.items())
        text = (
            f"# AFG31252 / {self.config.expected_serial} {interface_label(self.config)} 验证\n\n"
            f"{CONNECTION_NOTE}\n\n本批次：{self.folder.name}。全程禁止输出 ON、触发、复位和非易失存储写入。\n\n"
            f"| 阶段 | 结果 |\n| --- | --- |\n{rows}\n\n"
            f"错误：{self.report.get('error', self.report.get('control_error', '无'))}\n\n"
            "参数控制结果与物理输出验收分开；本次不测实际输出。初始/最终状态、恢复差异和逐项结果见 "
            "[results.json](results.json)，完整原始通信见 [communication.jsonl](communication.jsonl)。"
            "EMEM*-original.bin 保存原始编辑存储器，哈希见结果文件。\n"
        )
        (self.folder / "record.md").write_text(text, encoding="utf-8")
        from lab_control.storage.hardware_documents import refresh_after_batch

        refresh_after_batch(self.folder)


def capture_sources(folder: Path) -> dict[str, Any]:
    """Record the installed code as well as source checkouts; never require Git."""
    package_root = Path(__file__).resolve().parents[4]
    environment: dict[str, Any] = {"source_sha256": {}}
    sources = (
        Path(__file__).resolve().relative_to(package_root),
        Path("instruments/devices/tek_afg31252/adapter.py"),
        Path("instruments/devices/tek_afg31252/limits.py"),
        Path("instruments/registry.py"),
        Path("instruments/base.py"),
        Path("transport/visa.py"),
    )
    for source in sources:
        data = (package_root / source).read_bytes()
        saved = folder / "source/lab_control" / source
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_bytes(data)
        environment["source_sha256"][source.as_posix()] = hashlib.sha256(data).hexdigest()
    checkout = next(
        (p for p in package_root.parents if (p / ".git").exists() and p / "src/lab_control" == package_root),
        None,
    )
    if checkout:
        try:
            environment["project_head"] = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=checkout, text=True, stderr=subprocess.STDOUT
            ).strip()
            environment["working_tree"] = subprocess.check_output(
                ["git", "status", "--short"], cwd=checkout, text=True, stderr=subprocess.STDOUT
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            environment["git_metadata_error"] = str(exc)
    else:
        environment["project_head"] = "Installed package; code hashes recorded"
    return environment


def main(runner_type: type[Acceptance] = Acceptance, suffix: str = "usb-control") -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument(
        "--restore-from", type=Path, help="Restore a previous batch's verified initial snapshot"
    )
    args = parser.parse_args()
    configs = load_config(args.config)
    if len(configs) != 1:
        parser.error("Exactly one explicitly configured device is required")
    config = configs[0]
    if (
        config.adapter != "tek.afg31252"
        or config.transport != "visa"
        or not re.fullmatch(r"[A-Za-z0-9_-]+", config.expected_serial)
    ):
        parser.error("An exact AFG31252 VISA configuration and expected serial are required")
    try:
        interface = visa_interface(config)
    except ValueError as exc:
        parser.error(str(exc))
    suffix = suffix.replace("usb-", interface.lower() + "-", 1)
    asset = application_home() / "hardware-tests" / f"AFG31252-{config.expected_serial}"
    folder = asset / (
        datetime.now().strftime("%Y-%m-%d-%H%M%S-%f")
        + ("-preflight" if args.preflight_only else "-" + suffix)
    )
    folder.mkdir(parents=True, exist_ok=False)
    if args.restore_from and args.preflight_only:
        parser.error("Recovery and preflight-only are mutually exclusive")
    runner = runner_type(config, folder, args.preflight_only, args.restore_from)
    runner.report["environment"].update(capture_sources(folder))
    runner.save()
    runner.run()
    print(folder, flush=True)
    if runner.report.get("error") or runner.report.get("control_error"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
