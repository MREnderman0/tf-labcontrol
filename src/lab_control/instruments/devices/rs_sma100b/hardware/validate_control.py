"""Opt-in SMA100B RF-OFF acceptance through the registered model and session worker."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import re
import time
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Callable

import pyvisa

from lab_control.application.config import ConnectionConfig, load_config
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceCommand, jsonable, utc_now
from lab_control.instruments.devices.rs_sma100b.adapter import Sma100bAdapter, quote_name
from lab_control.instruments.registry import create_adapter
from lab_control.storage.repository import application_home

GROUP_PATHS = {
    "basic": (
        "SOUR:FREQ",
        "SOUR:POW",
        "OUTP",
        "SOUR:FREQ:MODE",
        "SOUR:ROSC:SOUR",
        "SOUR:POW:OFFS",
        "SOUR:POW:LIM",
        "FORM",
    ),
    "sweep": (
        "SOUR:FREQ:STAR",
        "SOUR:FREQ:STOP",
        "SOUR:SWE:FREQ:SPAC",
        "SOUR:SWE:FREQ:DWEL",
        "SOUR:SWE:FREQ:MODE",
        "SOUR:SWE:FREQ:STEP:LIN",
        "SOUR:SWE:FREQ:STEP:LOG",
        "SOUR:SWE:FREQ:SHAP",
        "SOUR:SWE:FREQ:RETR",
        "TRIG:FSW:SOUR",
    ),
    "modulation": (
        *(
            f"SOUR:{mode}{ch}:{suffix}"
            for mode in ("AM", "FM", "PM")
            for ch in (1, 2)
            for suffix in ("STAT", "SOUR", "DEPT" if mode == "AM" else "DEV")
        ),
        "SOUR:AM:TYPE",
        "SOUR:AM:DEV:MODE",
        "SOUR:FM:MODE",
        "SOUR:FM:DEV:MODE",
        "SOUR:PM:MODE",
        "SOUR:PM:DEV:MODE",
        "SOUR:LFO:FREQ:MODE",
        *(f"SOUR:LFO1:{suffix}" for suffix in ("STAT", "FREQ", "SHAP", "SOUR")),
        "SOUR:LFO2:STAT",  # Auxiliary output state is available; LF2 generator requires its option.
    ),
    "list": (
        "SOUR:LIST:CAT",
        "SOUR:LIST:SEL",
        "SOUR:LIST:MODE",
        "SOUR:LIST:TRIG:SOUR",
        "SOUR:LIST:DWEL",
        "SOUR:LIST:DWEL:MODE",
        "SOUR:LIST:IND",
        "SOUR:LIST:IND:STAR",
        "SOUR:LIST:IND:STOP",
    ),
}
SNAPSHOT_PATHS = tuple(path for paths in GROUP_PATHS.values() for path in paths)


def equivalent(a: Any, b: Any) -> bool:
    try:
        return math.isclose(float(a), float(b), rel_tol=1e-8, abs_tol=1e-9)
    except (ValueError, TypeError):
        return str(a).strip().upper() == str(b).strip().upper()


def differences(before: dict, after: dict) -> dict:
    return {
        key: {"expected": value, "actual": after.get(key)}
        for key, value in before.items()
        if key not in after or not equivalent(value, after[key])
    }


def save_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(jsonable(value), ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(20):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)  # Only local report replacement, never instrument replay.


class Guard:
    """Allow listed queries/settings only; independent ban on RF ON and triggers."""

    def __init__(self, inner, folder: Path):
        self.inner, self.resource, self.folder = inner, inner.resource, folder
        self.settings = False
        self.restore = False
        self.identity_ok = False
        self.writes = 0
        self.owned_lists: set[str] = set()
        self.selected = ""
        self.restore_values: dict[str, str] = {}
        self.allowed_queries: set[str] = set()
        self.allowed_settings: set[str] = set()

    def log(self, direction: str, data: str):
        with (self.folder / "communication.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"time": utc_now(), "direction": direction, "data": data}) + "\n")

    def open(self):
        raise RuntimeError("Session owns opening the resource")

    def write(self, payload):
        if not isinstance(payload, str) or any(c in payload for c in ";\r\n"):
            raise RuntimeError("Only single, explicit ASCII commands allowed")
        path, _, value = payload.partition(" ")
        if "?" in path:
            if payload not in self.allowed_queries:
                raise RuntimeError(f"Query not in manifest: {payload}")
        else:
            if path == "OUTP" and value == "OFF" and self.identity_ok:
                pass
            elif not self.settings or path not in self.allowed_settings:
                raise RuntimeError("Setting blocked: backup/manifest required")
            if path == "OUTP" and value not in {"OFF", "0"}:
                raise RuntimeError("RF ON forbidden")
            if path in {"SOUR:LFO1:STAT", "SOUR:LFO2:STAT"}:
                raise RuntimeError("Physical auxiliary output changes forbidden")
            if path in {"SOUR:POW:LIM", "SOUR:POW:OFFS"}:
                raise RuntimeError("Power limit and offset are read-only")
            if path == "SOUR:POW" and float(value) > 0:
                if not self.restore or not equivalent(value, self.restore_values.get(path)):
                    raise RuntimeError("Only the backed-up power may exceed 0 dBm during restoration")
            if path == "SOUR:LIST:SEL":
                self.selected = value.strip("'\"")
            if path in {"SOUR:LIST:FREQ", "SOUR:LIST:POW"} and self.selected not in self.owned_lists:
                raise RuntimeError("Cannot overwrite an existing list")
            if path == "SOUR:LIST:DEL" and value.strip("'\"") not in self.owned_lists:
                raise RuntimeError("Cannot delete an existing list")
            self.writes += 1
        self.log("tx", payload)
        self.inner.write(payload)

    def read(self):
        try:
            result = self.inner.read()
        except Exception as exc:
            self.log("error", str(exc))
            raise
        self.log("rx", result.decode("ascii", errors="backslashreplace"))
        return result

    def query(self, payload):
        self.write(payload)
        return self.read()

    def close(self):
        self.inner.close()


class Acceptance:
    def __init__(self, config: ConnectionConfig, folder: Path):
        self.config, self.folder = config, folder
        self.app: ApplicationRuntime | None = None
        self.device_id = ""
        self.guards: list[Guard] = []
        self.adapter: Sma100bAdapter | None = None
        self.initial: dict[str, str] = {}
        self.created_lists: set[str] = set()
        self.original_list: dict[str, Any] | None = None
        self.report: dict[str, Any] = {
            "started_at": utc_now(),
            "resource": config.resource,
            "expected_serial": config.expected_serial,
            "environment": {"python": platform.python_version(), "pyvisa": pyvisa.__version__},
            "stages": {
                name: "not_tested"
                for name in (
                    "connection",
                    "backup",
                    "basic",
                    "sweep",
                    "am",
                    "fm",
                    "pm",
                    "reference",
                    "list",
                    "list_mode",
                    "repeat_control",
                    "restoration",
                    "repeat_read",
                    "reconnection",
                    "physical_output",
                )
            },
            "preflight_queries": [],
            "operations": [],
            "restore_steps": [],
            "reconnections": [],
        }
        self.stage = "connection"

    def save(self):
        self.report["setting_write_count"] = sum(g.writes for g in self.guards)
        save_json(self.folder / "results.json", self.report)

    def factory(self, config):
        assert self.app is not None
        if config.adapter != self.config.adapter or config.resource != self.config.resource:
            raise RuntimeError("Only the explicitly configured asset is allowed")
        adapter = create_adapter(config, self.app.visa)
        assert isinstance(adapter, Sma100bAdapter)
        guard = Guard(adapter.transport, self.folder)
        guard.allowed_queries = {"*IDN?", "*OPT?", "SYST:ERR?", "SOUR:POW:OFFS?", "SOUR:POW:LIM?"}
        guard.allowed_queries.update(path + "?" for path in SNAPSHOT_PATHS)
        guard.allowed_queries.update(s.query for s in adapter.profile.commands.values() if s.query)
        guard.allowed_queries.update(
            f"SOUR:{path}? {limit}"
            for path in ("FM1:DEV", "PM1:DEV", "LFO1:FREQ")
            for limit in ("MIN", "MAX")
        )
        guard.allowed_queries.update(
            adapter.profile.commands[name].query + " " + limit
            for name in ("frequency", "power", "sweep_start", "sweep_stop")
            for limit in ("MIN", "MAX")
        )
        guard.allowed_queries.update(
            f"SOUR:LIST:{name}?"
            for name in ("FREQ", "POW", "DWEL:LIST", "FREQ:POIN", "POW:POIN", "DWEL:LIST:POIN")
        )
        guard.allowed_settings = set(SNAPSHOT_PATHS) - {"SOUR:LIST:CAT", "SOUR:POW:LIM", "SOUR:POW:OFFS"}
        guard.allowed_settings.update({"SOUR:LIST:FREQ", "SOUR:LIST:POW", "SOUR:LIST:DEL"})
        guard.allowed_settings.update(
            s.writes[0].split(" ")[0] for s in adapter.profile.commands.values() if s.writes
        )
        guard.allowed_settings = {
            p for p in guard.allowed_settings if not any(s in p for s in ("EXEC", "TRIG:IMM"))
        }
        adapter.transport = guard
        self.guards.append(guard)
        self.adapter = adapter
        return adapter

    def worker(self, action: Callable):
        assert self.app is not None
        return self.app.sessions.sessions[self.device_id].worker.submit(action).result(timeout=90)

    def query(self, query: str) -> str:
        assert self.adapter is not None
        return self.worker(partial(self.adapter.transport.query, query)).decode("ascii").strip()

    def errors(self) -> list[str]:
        errors: list[str] = []
        for _ in range(50):
            value = self.query("SYST:ERR?")
            if re.match(r"^\+?0(?:,|$)", value):
                return errors
            errors.append(value)
        raise RuntimeError("Error queue did not terminate within 50 responses")

    def snapshot(self, *, preflight=False, readonly_diagnostics=False) -> dict[str, str]:
        result = {}
        for path in SNAPSHOT_PATHS:
            response = self.query(path + "?")
            errors = self.errors()
            if preflight:
                self.report["preflight_queries"].append(
                    {"path": path, "response": response, "errors": errors}
                )
                self.save()
            if errors:
                if preflight:
                    continue
                if readonly_diagnostics:
                    self.report.setdefault("readonly_diagnostics", []).append(
                        {"time": utc_now(), "stage": self.stage, "query": path + "?", "errors": errors}
                    )
                else:
                    raise RuntimeError(f"{path}: {errors}")
            result[path] = response
        return result

    def connect(self):
        self.app = ApplicationRuntime((self.config,))
        self.app.sessions.factory = self.factory
        self.device_id = self.app.add(self.config)
        identity = self.app.connect(self.device_id).result(timeout=60)
        if identity.serial != self.config.expected_serial:
            raise RuntimeError("Exact configured serial did not match")
        self.report["identity"] = jsonable(identity)
        self.report["capabilities"] = jsonable(self.app.sessions.capability_snapshot(self.device_id))
        self.report["environment"]["visa"] = str(self.app.visa.manager.visalib)
        self.guards[-1].identity_ok = True
        self.report["stages"]["connection"] = "passed"

    def backup(self):
        self.stage = "backup"
        self.report["initial_errors"] = self.errors()
        self.initial = self.snapshot(preflight=True)
        self.report["initial_snapshot"] = self.initial
        self.report["blocked_groups"] = {
            group: [path for path in paths if path not in self.initial]
            for group, paths in GROUP_PATHS.items()
            if not set(paths).issubset(self.initial)
        }
        self.report["stages"]["backup"] = "passed" if not self.report["blocked_groups"] else "blocked"
        save_json(self.folder / "initial_snapshot.json", self.initial)
        self.save()
        assert self.adapter is not None
        if "list" not in self.report["blocked_groups"]:
            selected = self.initial["SOUR:LIST:SEL"].strip('"')
            self.guards[-1].allowed_queries.add("MMEM:DATA? " + quote_name(selected))
            data = self.worker(partial(self.adapter.backup_list_file, selected))
            (self.folder / "original-list.lsw").write_bytes(data)
            self.report["original_list_file_sha256"] = hashlib.sha256(data).hexdigest()
            self.original_list = self.worker(self.adapter.read_list)
            save_json(self.folder / "original-list.json", self.original_list)
            self.report["original_list_sha256"] = hashlib.sha256(
                (self.folder / "original-list.json").read_bytes()
            ).hexdigest()
            if self.errors():
                raise RuntimeError("List backup reported an instrument error")
        self.report["modulation_limits"] = {
            path: {limit: self.query(f"SOUR:{path}? {limit}") for limit in ("MIN", "MAX")}
            for path in ("FM1:DEV", "PM1:DEV", "LFO1:FREQ")
        }
        if self.errors():
            raise RuntimeError("Modulation range queries reported an instrument error")
        self.save()

    def outputs_off(self):
        state = {
            path: self.query(path + "?")
            for path in ("OUTP", "SOUR:LFO1:STAT", "SOUR:LFO2:STAT", "SOUR:POW:LIM", "SOUR:POW:OFFS")
        }
        if not equivalent(state["OUTP"], 0):
            raise RuntimeError("RF output is not OFF")
        for path in ("SOUR:LFO1:STAT", "SOUR:LFO2:STAT", "SOUR:POW:LIM", "SOUR:POW:OFFS"):
            if not equivalent(state[path], self.initial[path]):
                raise RuntimeError(f"Unintended auxiliary/limit change: {path}")
        return state

    def setting(self, path: str, value: Any):
        """Preparation/restoration only, still serialized and protected by the model guard."""
        assert self.adapter is not None
        self.outputs_off()
        text = str(value)
        if path == "SOUR:SWE:FREQ:STEP:LOG":
            text += "PCT"
        item = {
            "time": utc_now(),
            "stage": self.stage,
            "path": path,
            "requested": value,
            "kind": "preparation_or_restore",
        }
        self.report["operations"].append(item)
        self.save()
        self.worker(partial(self.adapter.transport.write, path + " " + text))
        actual = self.query(path + "?")
        errors = self.errors()
        item.update(actual=actual, errors=errors)
        if errors or not equivalent(value, actual):
            item["status"] = "failed"
            self.save()
            raise RuntimeError(f"{path}: requested {value}, actual {actual}, errors {errors}")
        item.update(status="passed", output=self.outputs_off())
        self.save()

    def apply(self, operation: str, value: Any = None, parameters: dict | None = None):
        assert self.app is not None
        item: dict[str, Any] = {
            "time": utc_now(),
            "stage": self.stage,
            "operation": operation,
            "requested": parameters or {"value": value},
            "kind": "adapter",
        }
        self.report["operations"].append(item)
        self.save()
        self.outputs_off()
        result = self.app.sessions.submit(
            self.device_id, "apply", DeviceCommand(operation, item["requested"], confirmed=True)
        ).result(timeout=90)
        item["result"] = jsonable(result)
        item["errors"] = self.errors()
        self.save()
        if result.outcome != "success" or item["errors"]:
            raise RuntimeError(f"{operation}: {result.outcome}, {result.error}, {item['errors']}")
        item.update(status="passed", output=self.outputs_off())
        self.save()
        return result

    def finish_group(self):
        self.report["stages"][self.stage] = "passed"
        self.save()
        print(self.stage + ": passed", flush=True)

    def control(self):
        if self.report["blocked_groups"]:
            raise RuntimeError("Complete backup required before this acceptance matrix")
        guard = self.guards[-1]
        guard.restore_values = self.initial.copy()
        guard.settings = True
        if not equivalent(self.initial["OUTP"], 0):
            assert self.adapter is not None
            self.worker(partial(self.adapter.transport.write, "OUTP OFF"))
        self.outputs_off()
        self.setting("SOUR:FREQ:MODE", "CW")
        for mode in ("AM", "FM", "PM"):
            for ch in (1, 2):
                if not equivalent(self.initial[f"SOUR:{mode}{ch}:STAT"], 0):
                    self.setting(f"SOUR:{mode}{ch}:STAT", 0)
        self.stage = "basic"
        self.apply("output", False)
        self.apply("power", -20)
        # Use the authorized internal-reference test during the control matrix.
        # Restore the original source only after the final control write to avoid
        # an absent external reference interrupting unrelated tests asynchronously.
        self.stage = "reference"
        self.apply("reference_clock", "INT")
        self.finish_group()
        self.stage = "basic"
        for frequency in (10e6, 100e6, 1e9, 3e9, 6e9, 12e9):
            for power in (-60, -40, -20, 0):
                self.apply("power", power)
                self.apply("frequency", frequency)
        self.finish_group()
        self.stage = "sweep"
        self.apply("power", -20)
        self.apply("frequency", 100e6)
        self.apply("sweep_mode", "MAN")
        self.apply("sweep_start", 100e6)
        self.apply("sweep_stop", 110e6)
        for spacing in ("LIN", "LOG"):
            self.apply("sweep_spacing", spacing)
            self.apply(
                "sweep_step_linear" if spacing == "LIN" else "sweep_step_log", 1e6 if spacing == "LIN" else 1
            )
            for dwell in (0.01, 0.02):
                self.apply("sweep_dwell", dwell)
                self.apply("frequency_mode", "SWE")
                self.apply("frequency_mode", "CW")
        self.finish_group()
        self.apply("frequency", 100e6)
        for mode, operation, values in (
            ("am", "am_depth", (10, 20)),
            ("fm", "fm_deviation", (1000, 10000)),
            ("pm", "pm_deviation", (0.1, 0.2)),
        ):
            self.stage = mode
            if any(not equivalent(self.initial[p], 0) for p in ("SOUR:LFO1:STAT", "SOUR:LFO2:STAT")):
                self.report["stages"][mode] = "blocked"
                continue
            self.setting(f"SOUR:{mode.upper()}:DEV:MODE", "UNC")
            if mode == "am":
                self.setting("SOUR:AM:TYPE", "LIN")
            self.setting("SOUR:LFO:FREQ:MODE", "CW")
            self.apply(mode + "_source", "INT")
            assert self.adapter is not None
            shape = self.worker(partial(self.adapter.read, mode + "_shape"))
            self.report.setdefault("shapes", {})[mode] = {"actual": shape, "scope": "read-only without K24"}
            if shape != "SIN":
                raise RuntimeError("This matrix requires the pre-existing sine LF waveform")
            for frequency in (100, 1000):
                self.apply(mode + "_frequency", frequency)
                for value in values:
                    self.apply(operation, value)
                    self.apply(mode + "_enabled", True)
                    self.apply(mode + "_enabled", False)
            self.finish_group()
        self.stage = "list"
        assert self.original_list is not None and self.adapter is not None
        suffix = datetime.now().strftime("%m%d%H%M%S")
        original_catalog = self.worker(self.adapter.list_catalog)
        self.setting("SOUR:LIST:TRIG:SOUR", "SING")
        self.setting("SOUR:LIST:MODE", "STEP")
        for index, (frequencies, powers) in enumerate(
            (([100e6, 101e6, 102e6], [-60, -40, -20]), ([1e9, 3e9, 6e9], [-50, -30, -10]))
        ):
            name = f"LC{suffix}_{index}"
            if name in original_catalog:
                raise RuntimeError("Temporary list already exists")
            self.created_lists.add(name)
            guard.owned_lists.add(name)
            self.report["created_lists"] = sorted(self.created_lists)
            self.save()
            self.apply("upload_list", parameters={"name": name, "frequencies": frequencies, "powers": powers})
            self.setting("SOUR:LIST:DWEL:MODE", "GLOB")
            self.setting("SOUR:LIST:IND:STAR", 0)
            self.setting("SOUR:LIST:IND:STOP", 2)
            for dwell in (0.01, 0.02):
                self.apply("list_dwell", dwell)
                reason = self.adapter.rf_off_list_mode_block_reason
                if reason:
                    self.report["stages"]["list_mode"] = "blocked"
                    self.report["list_mode_block_reason"] = reason
                else:
                    self.apply("frequency_mode", "LIST")
                    self.apply("frequency_mode", "CW")
                    self.report["stages"]["list_mode"] = "passed"
        self.worker(partial(self.adapter.select_existing_list, self.original_list["selected"]))
        self.verify_original_list()
        self.finish_group()
        self.stage = "repeat_control"
        for index in range(30):
            self.apply("frequency", 100e6 if index % 2 == 0 else 200e6)
            self.apply("power", -40 if index % 2 == 0 else -20)
        self.finish_group()

    def verify_original_list(self):
        assert self.adapter is not None and self.original_list is not None
        actual = self.worker(self.adapter.read_list)
        save_json(self.folder / "final-original-list.json", actual)
        digest = hashlib.sha256((self.folder / "final-original-list.json").read_bytes()).hexdigest()
        self.report["final_original_list_sha256"] = digest
        if actual != self.original_list or digest != self.report["original_list_sha256"]:
            raise RuntimeError("Original list data differs from backup")
        query = "MMEM:DATA? " + quote_name(self.original_list["selected"])
        self.guards[-1].allowed_queries.add(query)
        data = self.worker(partial(self.adapter.backup_list_file, self.original_list["selected"]))
        self.report["final_list_file_sha256"] = hashlib.sha256(data).hexdigest()
        if self.report["final_list_file_sha256"] != self.report["original_list_file_sha256"]:
            raise RuntimeError("Original list file hash changed")
        if self.errors():
            raise RuntimeError("Original list verification reported an instrument error")

    def restore(self):
        self.stage = "restoration"
        guard = self.guards[-1]
        guard.restore = True
        try:
            self.outputs_off()
            assert self.adapter is not None
            if self.query("*IDN?").strip() != self.report["identity"]["raw"].strip():
                raise RuntimeError("Identity changed before restoration")
            pending = self.errors()
            self.report["errors_before_restore"] = pending
            self.setting("SOUR:FREQ:MODE", "CW")
            for mode in ("AM", "FM", "PM"):
                for ch in (1, 2):
                    if not equivalent(self.query(f"SOUR:{mode}{ch}:STAT?"), 0):
                        self.setting(f"SOUR:{mode}{ch}:STAT", 0)
            if self.original_list and self.created_lists:
                self.worker(partial(self.adapter.select_existing_list, self.original_list["selected"]))
                self.verify_original_list()
                for name in sorted(self.created_lists):
                    if name in self.worker(self.adapter.list_catalog):
                        self.worker(partial(self.adapter.delete_created_list, name, self.created_lists))
                        if self.errors():
                            raise RuntimeError("Temporary list cleanup failed")
                if ",".join(self.worker(self.adapter.list_catalog)) != self.initial["SOUR:LIST:CAT"]:
                    raise RuntimeError("List catalogue was not restored")
            # Wide sweep endpoints first; all other dependent numeric values follow.
            stop = float(self.query("SOUR:FREQ:STOP?"))
            endpoints = (
                ("SOUR:FREQ:STOP", "SOUR:FREQ:STAR")
                if float(self.initial["SOUR:FREQ:STAR"]) > stop
                else ("SOUR:FREQ:STAR", "SOUR:FREQ:STOP")
            )
            late = {
                "SOUR:FREQ:MODE",
                "SOUR:FREQ",
                "SOUR:POW",
                "SOUR:ROSC:SOUR",
                *(f"SOUR:{m}{ch}:STAT" for m in ("AM", "FM", "PM") for ch in (1, 2)),
            }
            never_write = {
                "SOUR:LIST:CAT",
                "SOUR:LIST:SEL",
                "SOUR:POW:LIM",
                "SOUR:POW:OFFS",
                "OUTP",
                "SOUR:LFO1:STAT",
                "SOUR:LFO2:STAT",
            }
            ordered = [
                *endpoints,
                *(p for p in SNAPSHOT_PATHS if p not in late | never_write and p not in endpoints),
                "SOUR:FREQ",
                *(f"SOUR:{m}{ch}:STAT" for m in ("AM", "FM", "PM") for ch in (1, 2)),
                "SOUR:FREQ:MODE",
                "SOUR:ROSC:SOUR",
                "SOUR:POW",
            ]
            for path in ordered:
                current = self.query(path + "?")
                if not equivalent(current, self.initial[path]):
                    self.setting(path, self.initial[path])
                self.report["restore_steps"].append({"path": path, "status": "passed"})
                self.save()
            final = self.snapshot()
            expected = {**self.initial, "OUTP": "0"}
            self.report["final_snapshot"] = final
            self.report["restore_differences"] = differences(expected, final)
            save_json(self.folder / "final_snapshot.json", final)
            if self.report["restore_differences"]:
                raise RuntimeError("Restoration snapshot differs")
            self.verify_original_list()
            self.outputs_off()
            self.finish_group()
        except Exception as exc:
            self.report["restore_steps"].append({"status": "failed", "error": str(exc)})
            self.report["stages"]["restoration"] = "failed"
            self.report["final_state"] = (
                "Restoration not fully verified; final state may differ; see transcript"
            )
            self.save()
            # Stop restoration writes. A rejected setting may nevertheless have taken effect.
            # Verify the complete state without replaying anything; retain every error.
            try:
                final = self.snapshot(readonly_diagnostics=True)
                self.report["final_snapshot"] = final
                self.report["restore_differences"] = differences({**self.initial, "OUTP": "0"}, final)
                save_json(self.folder / "final_snapshot.json", final)
                if not self.report["restore_differences"]:
                    self.verify_original_list()
                    self.outputs_off()
                    self.report["restoration_warning"] = str(exc)
                    self.report["final_state"] = (
                        "Full original state verified by readback after restoration error; RF OFF"
                    )
                    self.finish_group()
                    return
            except Exception as verification_error:
                self.report["restoration_verification_error"] = str(verification_error)
                self.save()
            raise

    def repeat_read_and_reconnect(self):
        assert self.app is not None
        self.stage = "repeat_read"
        expected = self.report["final_snapshot"]
        readings = self.report["repeated_reads"] = []
        for index in range(600):
            values = {p: self.query(p + "?") for p in ("SOUR:FREQ", "SOUR:POW", "OUTP", "SOUR:FREQ:MODE")}
            if differences({p: expected[p] for p in values}, values):
                raise RuntimeError("Repeated read changed")
            readings.append({"time": utc_now(), "monotonic": time.monotonic(), "values": values})
            self.save()
            if (index + 1) % 30 == 0:
                print(f"repeat_read: {index + 1}/600", flush=True)
            if index != 599:
                time.sleep(1)
        self.finish_group()
        self.stage = "reconnection"
        for index in range(5):
            self.app.sessions.disconnect(self.device_id)
            identity = self.app.connect(self.device_id).result(timeout=60)
            actual = self.snapshot(readonly_diagnostics=True)
            item = {
                "attempt": index + 1,
                "identity": jsonable(identity),
                "differences": differences(expected, actual),
            }
            self.report["reconnections"].append(item)
            if jsonable(identity) != self.report["identity"] or item["differences"]:
                raise RuntimeError("Reconnection identity or settings differ")
            self.verify_original_list()
            self.save()
        self.finish_group()

    def run(self, preflight_only: bool):
        try:
            self.connect()
            self.backup()
            if not preflight_only:
                try:
                    self.control()
                except Exception as exc:
                    self.report["control_error"] = str(exc)
                    self.report["stages"][self.stage] = "failed"
                    print(f"control stopped: {exc}", flush=True)
                finally:
                    if any(g.writes for g in self.guards):
                        self.restore()
                if "control_error" not in self.report:
                    self.repeat_read_and_reconnect()
        except Exception as exc:
            self.report["error"] = str(exc)
            self.report["error_code"] = getattr(exc, "code", type(exc).__name__)
            self.report["stages"][self.stage] = (
                "blocked" if getattr(exc, "code", "") == "process_busy" else "failed"
            )
        finally:
            if self.app:
                self.app.close()
                self.report["session_released"] = True
            self.report["finished_at"] = utc_now()
            self.save()
            self.record()

    def record(self):
        rows = "\n".join(f"| {name} | {value} |" for name, value in self.report["stages"].items())
        (self.folder / "record.md").write_text(
            f"# SMA100B / {self.config.expected_serial} — RF OFF 控制验证\n\n"
            f"时间：{self.report['started_at']}；资源：`{self.config.resource}`。\n\n"
            f"| 项目 | 结果 |\n| --- | --- |\n{rows}\n\n"
            f"错误：{self.report.get('error', self.report.get('control_error', '无'))}。设置写入数：{self.report['setting_write_count']}。\n\n"
            f"恢复诊断：{self.report.get('restoration_warning', '无')}。\n\n"
            f"列表模式限制：{self.report.get('list_mode_block_reason', '无')}。\n\n"
            "RF 开启、触发执行、物理输出及精度未验收。缺选件/无法备份的组不能标为通过。"
            "完整请求、读回、错误及恢复结果见 [results.json](results.json) 和 [communication.jsonl](communication.jsonl)。\n",
            encoding="utf-8",
        )
        index = self.folder.parent.parent / "index.md"
        old = index.read_text(encoding="utf-8-sig") if index.exists() else "# 实机台账\n"
        link = f"{self.folder.parent.name}/{self.folder.name}/record.md"
        if link not in old:
            index.write_text(
                old.rstrip()
                + f"\n\n- {self.folder.name}：SMA100B，{self.report['stages']}；[记录]({link})\n",
                encoding="utf-8",
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--asset-name", required=True)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    configs = load_config(args.config)
    if (
        len(configs) != 1
        or configs[0].adapter != "rs.sma100b"
        or configs[0].transport != "visa"
        or not configs[0].expected_serial
    ):
        parser.error("Require one exact SMA100B VISA configuration with full expected serial")
    if not re.fullmatch(r"SMA100B-[A-Za-z0-9_-]+", args.asset_name):
        parser.error("Require a safe SMA100B asset directory name")
    folder = (
        application_home()
        / "hardware-tests"
        / args.asset_name
        / (
            datetime.now().strftime("%Y-%m-%d-%H%M%S-%f")
            + ("-preflight" if args.preflight_only else "-rf-off-control")
        )
    )
    folder.mkdir(parents=True, exist_ok=False)
    runner = Acceptance(configs[0], folder)
    for path in Path(__file__).parents[1].rglob("*.py"):
        if "tests" in path.parts:
            continue
        relative = path.relative_to(Path(__file__).parents[1])
        saved = folder / "source" / relative
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_bytes(path.read_bytes())
        runner.report.setdefault("source_sha256", {})[relative.as_posix()] = hashlib.sha256(
            saved.read_bytes()
        ).hexdigest()
    runner.save()
    runner.run(args.preflight_only)
    print(folder, flush=True)
    print(json.dumps(runner.report["stages"]), flush=True)
    return int(bool(runner.report.get("error") or runner.report.get("control_error")))


if __name__ == "__main__":
    raise SystemExit(main())
