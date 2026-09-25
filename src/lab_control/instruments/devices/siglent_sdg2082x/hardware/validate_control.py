"""Explicit SDG2082X USB/LAN control acceptance; both physical outputs remain OFF."""

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
from pathlib import Path
from typing import Any, Callable

import pyvisa

from lab_control.application.config import ConnectionConfig, load_config
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceCommand, DeviceError, jsonable, utc_now
from lab_control.instruments.common.hardware_acceptance import (
    enumerate_configured,
    interface_label,
    visa_interface,
)
from lab_control.instruments.devices.siglent_sdg2082x.adapter import Sdg2082xAdapter, output_state
from lab_control.instruments.devices.siglent_sdg2082x.protocol import fields
from lab_control.instruments.registry import create_adapter
from lab_control.storage.repository import application_home

QUERIES = {"*IDN?", "COUP?", "STL? USER"} | {
    f"C{ch}:{group}?"
    for ch in (1, 2)
    for group in ("BSWV", "OUTP", "MDWV", "SWWV", "BTWV", "ARWV", "SRATE", "SYNC")
}
BASE_KEYS = ("FRQ", "AMP", "OFST", "PHSE", "SYM", "WIDTH", "RISE", "FALL", "DLY", "DUTY")
GROUP_KEYS = {
    "MDWV": ("MDSP", "FRQ", "DEPTH", "DEVI", "SRC"),
    "SWWV": ("START", "STOP", "TIME", "SWMD", "DIR", "TRSR"),
    "BTWV": ("GATE_NCYC", "TIME", "PRD", "DLAY", "STPS", "TRSR"),
}


def save_json(path: Path, data: Any) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(jsonable(data), ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(20):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


def equivalent(a: Any, b: Any) -> bool:
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(equivalent(a[k], b[k]) for k in a)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=1e-7, abs_tol=1e-10)
    return a == b


def require_complete_backup(group: str, values: dict, mode: str = "") -> None:
    """Do not change parameters whose inactive-source values were never backed up."""
    required = set(GROUP_KEYS[group]) - {"DEVI" if mode == "AM" else "DEPTH"}
    if not required <= values.keys() or "CARR" not in values:
        raise RuntimeError(f"Incomplete {group}/{mode} backup; parameter tests are not permitted")
    if group == "MDWV" and values.get("SRC") != "INT":
        raise RuntimeError("Internal modulation settings are hidden; stop before changing parameters")
    if group == "BTWV" and (values.get("GATE_NCYC"), values.get("TRSR")) != ("NCYC", "INT"):
        raise RuntimeError(
            "Count/period backup requires original NCYC/INT; parameter tests are not permitted"
        )


class Guard:
    """One-command allow list; never reset, trigger, enable output or overwrite files."""

    def __init__(self, inner, folder: Path):
        self.inner, self.resource, self.folder = inner, inner.resource, folder
        self.settings = False
        self.writes = 0
        self.allowed_settings: set[str] = set()

    def open(self):
        raise RuntimeError("The session owns opening this transport")

    def log(self, direction: str, value: str):
        with (self.folder / "communication.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"time": utc_now(), "direction": direction, "data": value}) + "\n")

    def write(self, payload):
        if not isinstance(payload, str) or any(c in payload for c in ";\r\n"):
            raise DeviceError("Acceptance forbids binary/unlisted commands", "unsupported")
        if payload not in QUERIES:
            if not self.settings or payload not in self.allowed_settings:
                raise DeviceError(f"Setting not authorized by acceptance: {payload}", "unsupported")
            if re.search(r"OUTP|SYNC|TRMD|MTRIG|\*|LOAD|PLRT", payload):
                raise DeviceError("Physical output/trigger/global actions forbidden", "unsupported")
            if not re.match(r"C[12]:(BSWV|MDWV|SWWV|BTWV) ", payload):
                raise DeviceError("Only explicit model parameter settings allowed", "unsupported")
            self.writes += 1
        self.log("tx", payload)
        self.inner.write(payload)

    def read(self):
        try:
            raw = self.inner.read()
        except Exception as exc:
            self.log("error", str(exc) or type(exc).__name__)
            raise
        self.log("rx", raw.decode("ascii", errors="backslashreplace"))
        return raw

    def query(self, payload):
        self.write(payload)
        return self.read()

    def close(self):
        self.inner.close()


class Acceptance:
    def __init__(self, config: ConnectionConfig, folder: Path):
        self.config, self.folder = config, folder
        self.app: ApplicationRuntime | None = None
        self.adapter: Sdg2082xAdapter | None = None
        self.device_id = ""
        self.guards: list[Guard] = []
        self.initial: dict[str, Any] = {}
        self.backups: dict[str, Any] = {}
        self.stage = "connection"
        self.dirty = False
        self.report: dict[str, Any] = {
            "started_at": utc_now(),
            "resource": config.resource,
            "expected_serial": config.expected_serial,
            "scope": f"{interface_label(config)} controls with both outputs OFF; no physical waveform measurement",
            "environment": {"python": platform.python_version(), "pyvisa": pyvisa.__version__},
            "stages": {
                key: "not_tested"
                for key in (
                    "identity",
                    "connection",
                    "backup",
                    "basic",
                    "am",
                    "fm",
                    "pm",
                    "sweep",
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

    def save(self):
        self.report["setting_writes"] = sum(g.writes for g in self.guards)
        self.report["backups"] = self.backups
        save_json(self.folder / "results.json", self.report)
        from lab_control.instruments.devices.siglent_sdg2082x.hardware.records import publish_report

        publish_report(self.folder)

    def worker(self, action: Callable):
        assert self.app is not None
        return self.app.sessions.sessions[self.device_id].worker.submit(action).result(timeout=90)

    def query(self, command: str) -> str:
        assert self.adapter is not None
        transport = self.adapter.transport
        return self.worker(lambda: transport.query(command)).decode("ascii").strip()

    def data(self, ch: int, group: str) -> dict:
        return fields(self.query(f"C{ch}:{group}?").encode())

    def factory(self, config):
        assert self.app is not None
        if config != self.config:
            raise RuntimeError("Only the explicit asset is authorized")
        adapter = create_adapter(config, self.app.visa)
        if not isinstance(adapter, Sdg2082xAdapter):
            adapter.close()
            raise RuntimeError("Model factory mismatch")
        guard = Guard(adapter.transport, self.folder)
        adapter.transport = guard
        self.guards.append(guard)
        self.adapter = adapter
        return adapter

    def connect(self):
        self.app = ApplicationRuntime((self.config,), root=self.folder / "audit")
        self.app.sessions.factory = self.factory
        self.device_id = self.app.add(self.config)
        self.report["enumeration"] = list(enumerate_configured(self.app.visa, self.config))
        self.report["stages"]["enumeration"] = (
            "passed"
            if self.config.resource.upper() in {r.upper() for r in self.report["enumeration"]}
            else "blocked"
        )
        identity = self.app.connect(self.device_id, configure_acquisition=False).result(timeout=60)
        self.report["identity"] = jsonable(identity)
        self.report["environment"]["visa"] = str(self.app.visa.manager.visalib)
        self.report["stages"].update(identity="passed", connection="passed")

    def snapshot(self) -> dict[str, str]:
        return {query: self.query(query) for query in sorted(QUERIES - {"*IDN?"})}

    def channel_snapshot(self, ch: int) -> dict[str, str]:
        return {query: self.query(query) for query in sorted(QUERIES) if query.startswith(f"C{ch}:")}

    def outputs_off(self):
        for ch in (1, 2):
            if output_state(self.query(f"C{ch}:OUTP?").encode()) != "OFF":
                raise RuntimeError("Output is not OFF; no further settings allowed")
            if output_state(self.query(f"C{ch}:SYNC?").encode()) != "OFF":
                raise RuntimeError("Sync output is not OFF; no further settings allowed")

    def raw_setting(self, command: str):
        assert self.adapter is not None
        self.outputs_off()
        guard = self.guards[-1]
        guard.allowed_settings.add(command)
        guard.settings = True
        self.dirty = True
        entry = {"time": utc_now(), "stage": self.stage, "command": command}
        self.report["restore_steps" if self.stage == "restoration" else "operations"].append(entry)
        self.save()
        try:
            transport = self.adapter.transport
            self.worker(lambda: transport.write(command))
            entry["status"] = "sent"
            self.outputs_off()
        except Exception as exc:
            entry.update(status="failed", error=str(exc) or type(exc).__name__)
            raise
        finally:
            guard.allowed_settings.discard(command)
            self.save()

    def set_field(self, ch: int, group: str, key: str, value: Any, mode=""):
        current = self.data(ch, group)
        if equivalent(current.get(key), value):
            return
        wire = f"{value:.12g}" if isinstance(value, float) else str(value)
        self.raw_setting(f"C{ch}:{group} {mode + ',' if mode else ''}{key},{wire}")
        actual = self.data(ch, group)
        if not equivalent(actual.get(key), value):
            raise RuntimeError(
                f"Restore/readback mismatch {ch}/{group}/{key}: {value!r} vs {actual.get(key)!r}"
            )

    def state(self, ch: int, group: str, enabled: bool):
        self.set_field(ch, group, "STATE", "ON" if enabled else "OFF")
        current = self.data(ch, group)
        if enabled and current.get("TRMD", "OFF") != "OFF":
            self.raw_setting(f"C{ch}:{group} STATE,OFF")
            raise RuntimeError("Stored trigger-output mode is enabled; advanced group stopped")

    def quiet(self, ch: int):
        for group in ("MDWV", "SWWV", "BTWV"):
            self.state(ch, group, False)

    def preflight(self, restoring=False):
        self.initial = self.snapshot()
        self.report["initial_snapshot"] = self.initial
        self.save()
        self.outputs_off()
        coupling = fields(self.initial["COUP?"].encode())
        if any(coupling.get(key) != "OFF" for key in ("TRACE", "FCOUP", "PCOUP", "ACOUP")):
            raise RuntimeError("Disable tracking/coupling before this independent-channel acceptance")
        for ch in (1, 2):
            basic = fields(self.initial[f"C{ch}:BSWV?"].encode())
            if basic.get("WVTP") not in {"SINE", "SQUARE", "RAMP", "PULSE"}:
                raise RuntimeError("This acceptance needs a backed-up standard periodic initial waveform")
            if basic["FRQ"] > 1e6:
                raise RuntimeError("Initial frequency cannot be transferred safely to every tested waveform")
            for group in ("MDWV", "SWWV", "BTWV"):
                if not restoring and fields(self.initial[f"C{ch}:{group}?"].encode()).get("STATE") != "OFF":
                    raise RuntimeError("Initial advanced modes must be OFF for this acceptance")

    def backup(self):
        self.stage = "backup"
        for ch in (1, 2):
            base = self.data(ch, "BSWV")
            self.backups[f"{ch}.base"] = base
            self.save()
            for kind in ("SINE", "SQUARE", "PULSE", "RAMP"):
                self.set_field(ch, "BSWV", "WVTP", kind)
                self.backups[f"{ch}.wave.{kind}"] = self.data(ch, "BSWV")
                self.save()
            self.restore_base(ch, base)
            self.state(ch, "MDWV", True)
            original = self.data(ch, "MDWV")
            self.backups[f"{ch}.modulation_original"] = original
            self.save()
            if original.get("MODE") not in {"AM", "FM", "PM"}:
                raise RuntimeError(
                    "Initial hidden modulation type is outside this acceptance's restore scope"
                )
            for mode in ("AM", "FM", "PM"):
                if self.data(ch, "MDWV").get("MODE") != mode:
                    self.raw_setting(f"C{ch}:MDWV {mode}")
                self.backups[f"{ch}.mod.{mode}"] = self.data(ch, "MDWV")
                self.save()
                require_complete_backup("MDWV", self.backups[f"{ch}.mod.{mode}"], mode)
            self.raw_setting(f"C{ch}:MDWV {original['MODE']}")
            self.state(ch, "MDWV", False)
            for group in ("SWWV", "BTWV"):
                self.state(ch, group, True)
                self.backups[f"{ch}.{group}"] = self.data(ch, group)
                self.save()
                require_complete_backup(group, self.backups[f"{ch}.{group}"])
                self.state(ch, group, False)
            self.restore_base(ch, base)
        self.report["stages"]["backup"] = "passed"
        self.save()

    def restore_base(self, ch: int, target: dict):
        self.set_field(ch, "BSWV", "WVTP", target["WVTP"])
        keys = [key for key in BASE_KEYS if key in target]
        if target["WVTP"] == "PULSE" and "WIDTH" in keys:
            keys.remove("DUTY") if "DUTY" in keys else None
        # Conservative transition before restoring a larger amplitude or offset.
        if not equivalent(self.data(ch, "BSWV").get("OFST"), target.get("OFST")):
            self.set_field(ch, "BSWV", "OFST", 0.0)
        for key in keys:
            self.set_field(ch, "BSWV", key, target[key])
        actual = self.data(ch, "BSWV")
        if not equivalent(target, actual):
            self.report.setdefault("basic_restore_differences", []).append(
                {"channel": ch, "expected": target, "actual": actual}
            )
            self.save()
            raise RuntimeError(f"Complete basic waveform restoration mismatch: {ch}/{target['WVTP']}")

    def apply(self, ch: int, operation: str, value: Any):
        assert self.app is not None and self.adapter is not None
        self.outputs_off()
        other = self.channel_snapshot(3 - ch)
        command = DeviceCommand(operation, {"value": value}, ch, True)
        spec, parameters = self.adapter.validate_command(command)
        context = self.adapter._context(ch, parameters)
        payloads = self.adapter.encode(operation, spec, context, parameters)
        guard = self.guards[-1]
        guard.settings = True
        guard.allowed_settings.update(p for p in payloads if isinstance(p, str))
        entry = {
            "time": utc_now(),
            "channel": ch,
            "operation": operation,
            "requested": value,
            "stage": self.stage,
        }
        self.report["operations"].append(entry)
        self.dirty = True
        self.save()
        try:
            result = self.app.sessions.submit(self.device_id, "apply", command).result(timeout=60)
            entry["result"] = jsonable(result)
            if result.outcome != "success":
                raise RuntimeError(f"{operation}: {result.outcome}: {result.error}")
            self.outputs_off()
            other_after = self.channel_snapshot(3 - ch)
            entry["other_channel_before"] = other
            entry["other_channel_after"] = other_after
            entry["other_channel_unchanged"] = equivalent(other, other_after)
            if not entry["other_channel_unchanged"]:
                raise RuntimeError("Other channel changed")
        finally:
            guard.allowed_settings.difference_update(p for p in payloads if isinstance(p, str))
            self.save()

    def group(self, name: str, action: Callable):
        self.stage = name
        try:
            action()
        except Exception:
            self.report["stages"][name] = "failed"
            raise
        else:
            self.report["stages"][name] = "passed"
        finally:
            self.save()

    def basic(self):
        value: Any
        for ch in (1, 2):
            self.quiet(ch)
            self.apply(ch, "frequency", 1000)
            self.apply(ch, "offset", 0)
            self.apply(ch, "amplitude", 0.2)
            for kind in ("SINE", "SQUARE", "PULSE", "RAMP"):
                self.apply(ch, "waveform", kind)
                if kind != "PULSE":
                    for value in (0, 90):
                        self.apply(ch, "phase", value)
                if kind in {"SQUARE", "PULSE"}:
                    for value in (25, 75):
                        self.apply(ch, "pulse_duty", value)
                if kind == "PULSE":
                    self.apply(ch, "pulse_width", 0.00025)
                if kind == "RAMP":
                    for value in (25, 75):
                        self.apply(ch, "ramp_symmetry", value)
            self.apply(ch, "waveform", "SINE")
            for value in (-0.05, 0.05, 0):
                self.apply(ch, "offset", value)

    def modulation(self, mode: str):
        for ch in (1, 2):
            self.quiet(ch)
            self.apply(ch, "waveform", "SINE")
            self.apply(ch, "frequency", 10000)
            self.apply(ch, "amplitude", 0.2)
            self.apply(ch, "modulation", True)
            self.apply(ch, "modulation_type", mode)
            self.apply(ch, mode.lower() + "_source", "INT")
            self.apply(ch, mode.lower() + "_shape", "SINE")
            for frequency in (100, 1000):
                self.apply(ch, mode.lower() + "_frequency", frequency)
                for value in (100, 200) if mode == "FM" else (10, 20):
                    self.apply(ch, mode.lower() + ("_depth" if mode == "AM" else "_deviation"), value)
            self.apply(ch, "modulation", False)

    def sweep(self):
        for ch in (1, 2):
            self.quiet(ch)
            self.apply(ch, "sweep", True)
            self.apply(ch, "sweep_trigger", "MAN")
            current = self.data(ch, "SWWV")
            if current["START"] > 2000:
                self.apply(ch, "sweep_start", 1000)
            self.apply(ch, "sweep_stop", 2000)
            self.apply(ch, "sweep_start", 1000)
            for spacing in ("LINE", "LOG"):
                self.apply(ch, "sweep_spacing", spacing)
                for duration in (1, 2):
                    self.apply(ch, "sweep_time", duration)
            for direction in ("UP", "DOWN"):
                self.apply(ch, "sweep_direction", direction)
            self.apply(ch, "sweep_trigger", "EXT")
            self.apply(ch, "sweep", False)

    def burst(self):
        value: Any
        for ch in (1, 2):
            self.quiet(ch)
            self.apply(ch, "burst", True)
            self.apply(ch, "burst_trigger", "MAN")
            self.apply(ch, "burst_mode", "NCYC")
            for value in (1, 5):
                self.apply(ch, "burst_cycles", value)
            self.report["burst_period_test"] = {
                "status": "not_tested",
                "reason": "Firmware omits PRD with MAN/EXT trigger; test scope excludes internal triggering",
            }
            for value in (0.001, 0.002):
                self.apply(ch, "burst_delay", value)
            self.apply(ch, "burst_mode", "GATE")
            self.apply(ch, "burst_trigger", "EXT")
            self.apply(ch, "burst", False)

    def repeat_control(self):
        for ch in (1, 2):
            self.quiet(ch)
            self.apply(ch, "waveform", "SINE")
        for i in range(30):
            for ch in (1, 2):
                self.apply(ch, "frequency", 1000 if i % 2 == 0 else 2000)
                self.apply(ch, "amplitude", 0.1 if i % 2 == 0 else 0.2)

    def restore_group(self, ch: int, group: str, target: dict, mode=""):
        self.state(ch, group, True)
        if mode and self.data(ch, group).get("MODE") != mode:
            self.raw_setting(f"C{ch}:MDWV {mode}")
        keys = list(GROUP_KEYS[group])
        if group == "MDWV":
            self.set_field(ch, group, "SRC", "INT", mode)
        if group == "BTWV":
            self.set_field(ch, group, "GATE_NCYC", "NCYC")
            self.set_field(ch, group, "TRSR", target["TRSR"])
            keys.remove("GATE_NCYC")
            keys.append("GATE_NCYC")
        if group == "SWWV" and target["START"] > self.data(ch, group)["STOP"]:
            keys.remove("STOP")
            keys.insert(0, "STOP")
        for key in keys:
            if key in target:
                self.set_field(ch, group, key, target[key], mode)
        if "CARR" in target:
            carrier = target["CARR"]
            for key in ("WVTP", *BASE_KEYS):
                if key not in carrier or key == "WIDTH":
                    continue
                current = self.data(ch, group).get("CARR", {})
                if equivalent(current.get(key), carrier[key]):
                    continue
                value = carrier[key]
                wire = f"{value:.12g}" if isinstance(value, float) else str(value)
                self.raw_setting(f"C{ch}:{group} CARR,{key},{wire}")
                if not equivalent(self.data(ch, group).get("CARR", {}).get(key), value):
                    raise RuntimeError(f"Carrier restoration mismatch: {ch}/{group}/{key}")
        actual = self.data(ch, group)
        matched = equivalent(target, actual)
        self.report.setdefault("restored_groups", []).append(
            {
                "channel": ch,
                "group": group,
                "mode": mode,
                "expected": target,
                "actual": actual,
                "matched": matched,
            }
        )
        self.save()
        if not matched:
            raise RuntimeError(f"Complete group restoration mismatch: {ch}/{group}/{mode}")
        self.state(ch, group, False)

    def restore(self):
        self.stage = "restoration"
        for ch in (1, 2):
            if f"{ch}.base" not in self.backups:
                continue
            self.quiet(ch)
            for kind in ("SINE", "SQUARE", "PULSE", "RAMP"):
                if f"{ch}.wave.{kind}" in self.backups:
                    self.restore_base(ch, self.backups[f"{ch}.wave.{kind}"])
            for mode in ("AM", "FM", "PM"):
                if f"{ch}.mod.{mode}" in self.backups:
                    self.restore_group(ch, "MDWV", self.backups[f"{ch}.mod.{mode}"], mode)
            original = self.backups.get(f"{ch}.modulation_original")
            if original:
                self.state(ch, "MDWV", True)
                if self.data(ch, "MDWV").get("MODE") != original["MODE"]:
                    self.raw_setting(f"C{ch}:MDWV {original['MODE']}")
                self.state(ch, "MDWV", False)
            for group in ("SWWV", "BTWV"):
                if f"{ch}.{group}" in self.backups:
                    self.restore_group(ch, group, self.backups[f"{ch}.{group}"])
            self.restore_base(ch, self.backups[f"{ch}.base"])
        final = self.snapshot()
        self.report["final_snapshot"] = final
        differences = {
            key: {"expected": value, "actual": final.get(key)}
            for key, value in self.initial.items()
            if final.get(key) != value
        }
        self.report["restoration_differences"] = differences
        if differences:
            raise RuntimeError("Final snapshot differs from the original; see restoration_differences")
        self.report["stages"]["restoration"] = "passed"
        self.dirty = False
        self.save()

    def stability(self):
        self.stage = "repeat_read"
        self.report["stable_reads"] = []
        for index in range(60):
            started = time.monotonic()
            values = {f"C{ch}:BSWV?": self.query(f"C{ch}:BSWV?") for ch in (1, 2)}
            self.outputs_off()
            if any(value != self.initial[key] for key, value in values.items()):
                raise RuntimeError("Restored settings changed during repeat reading")
            self.report["stable_reads"].append(
                {"time": utc_now(), "sample_started_monotonic": started, "values": values}
            )
            self.save()
            # Wait after the completed read: worker scheduling can shift query
            # start times even when the outer loop starts every second.
            if index < 59:
                time.sleep(1)
        self.report["stages"]["repeat_read"] = "passed"
        self.stage = "reconnection"
        assert self.app is not None
        for _ in range(3):
            self.app.sessions.disconnect(self.device_id)
            identity = self.app.connect(self.device_id, configure_acquisition=False).result(timeout=30)
            actual = self.snapshot()
            if actual != self.initial:
                raise RuntimeError("Reconnection snapshot mismatch")
            self.report["reconnections"].append(
                {"time": utc_now(), "identity": jsonable(identity), "snapshot": actual}
            )
            self.save()
        self.report["stages"]["reconnection"] = "passed"

    def run(
        self, preflight_only=False, restore_from: Path | None = None, backup_only=False, verify_only=False
    ):
        try:
            self.connect()
            self.preflight(restoring=bool(restore_from))
            if restore_from:
                previous = json.loads((restore_from / "results.json").read_text(encoding="utf-8"))
                if (
                    previous["expected_serial"] != self.config.expected_serial
                    or previous["resource"] != self.config.resource
                ):
                    raise RuntimeError("Restore batch belongs to another asset/resource")
                self.initial, self.backups = previous["initial_snapshot"], previous["backups"]
                self.report["restore_from"] = str(restore_from)
                self.report["restore_target_snapshot"] = self.initial
                self.restore()
            elif verify_only:
                self.stability()
            elif not preflight_only:
                self.backup()
                if backup_only:
                    self.restore()
                    return
                self.group("basic", self.basic)
                for mode in ("AM", "FM", "PM"):
                    self.group(mode.lower(), lambda mode=mode: self.modulation(mode))
                self.group("sweep", self.sweep)
                self.group("burst", self.burst)
                self.report["stages"]["arbitrary"] = "blocked"
                self.report["arbitrary_reason"] = (
                    "Official SDG2000X waveform upload/readback is documented, but a model-specific remote cleanup command is not confirmed. No files uploaded or deleted."
                )
                self.group("repeat_control", self.repeat_control)
                self.restore()
                self.stability()
        except Exception as exc:
            self.report["error"] = str(exc) or type(exc).__name__
            self.report["stages"][self.stage] = "failed"
        finally:
            if self.dirty and self.app and self.adapter and self.stage != "restoration":
                try:
                    self.report["state_before_recovery"] = self.snapshot()
                    self.save()
                    self.restore()
                except Exception as exc:
                    self.report["restoration_error"] = str(exc) or type(exc).__name__
                    self.report["stages"]["restoration"] = "failed"
            if self.app:
                try:
                    if self.adapter:
                        self.report["final_observed_snapshot"] = self.snapshot()
                except Exception as exc:
                    self.report["final_snapshot_error"] = str(exc) or type(exc).__name__
                try:
                    self.app.close()
                    self.report["session_released"] = True
                except Exception as exc:
                    self.report["close_error"] = str(exc) or type(exc).__name__
                    self.report["session_released"] = False
            self.report["finished_at"] = utc_now()
            self.save()
            rows = "\n".join(f"| {key} | {value} |" for key, value in self.report["stages"].items())
            (self.folder / "record.md").write_text(
                f"# SDG2082X / {self.config.expected_serial} 输出关闭控制验收\n\n"
                f"| 阶段 | 结果 |\n| --- | --- |\n{rows}\n\n"
                f"错误：{self.report.get('error', '无')}\n\n"
                f"恢复错误：{self.report.get('restoration_error', '无')}\n\n"
                f"任意波形：{self.report.get('arbitrary_reason', '未测试')}\n\n"
                f"突发周期：{self.report.get('burst_period_test', '未测试')}\n\n"
                "实际输出与精度未验收。最终状态与差异以 [results.json](results.json) 为准；"
                "原始通信见 [communication.jsonl](communication.jsonl)。\n",
                encoding="utf-8",
            )
            from lab_control.storage.hardware_documents import refresh_after_batch

            refresh_after_batch(self.folder)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument(
        "--verify-only", action="store_true", help="Read stability and reconnect, zero settings"
    )
    parser.add_argument(
        "--backup-only",
        action="store_true",
        help="Controlled mode snapshots followed by verified restoration",
    )
    parser.add_argument("--restore-from", type=Path)
    args = parser.parse_args()
    configs = load_config(args.config)
    if (
        len(configs) != 1
        or configs[0].adapter != "siglent.sdg2082x"
        or configs[0].transport != "visa"
        or not re.fullmatch(r"[A-Za-z0-9_-]+", configs[0].expected_serial)
    ):
        parser.error("Require one exact SDG2082X VISA configuration with expected serial")
    try:
        interface = visa_interface(configs[0])
    except ValueError as exc:
        parser.error(str(exc))
    if sum((args.preflight_only, args.backup_only, args.verify_only, bool(args.restore_from))) > 1:
        parser.error("Choose only one of preflight, backup, verify or restore")
    suffix = (
        "preflight"
        if args.preflight_only
        else "verify"
        if args.verify_only
        else "backup"
        if args.backup_only
        else "restore"
        if args.restore_from
        else "advanced-control"
    )
    folder = (
        application_home()
        / "hardware-tests"
        / f"SDG2082X-{configs[0].expected_serial}"
        / (datetime.now().strftime("%Y-%m-%d-%H%M%S-%f") + "-" + interface.lower() + "-" + suffix)
    )
    folder.mkdir(parents=True, exist_ok=False)
    runner = Acceptance(configs[0], folder)
    project = Path(__file__).resolve().parents[6]
    runner.report["environment"]["runtime_home"] = str(application_home())
    runner.report["environment"]["config_path"] = str(args.config.resolve())
    try:
        runner.report["environment"]["git_head"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=project, text=True, encoding="utf-8", timeout=10
        ).strip()
        runner.report["environment"]["git_status"] = subprocess.check_output(
            ["git", "status", "--short"], cwd=project, text=True, encoding="utf-8", timeout=10
        )
    except (OSError, subprocess.SubprocessError) as exc:
        runner.report["environment"]["git_unavailable"] = str(exc)
    for path in Path(__file__).parents[1].rglob("*.py"):
        if "tests" in path.parts:
            continue
        relative = path.relative_to(Path(__file__).parents[1])
        target = folder / "source" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        runner.report.setdefault("source_sha256", {})[relative.as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    runner.save()
    print(folder, flush=True)
    runner.run(args.preflight_only, args.restore_from, args.backup_only, args.verify_only)
    print(json.dumps(runner.report["stages"]), flush=True)
    return int(
        any(
            runner.report.get(key)
            for key in ("error", "restoration_error", "close_error", "final_snapshot_error")
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
