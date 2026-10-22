"""Opt-in read-only model verification with per-asset records and process lease.

Model packages provide the definition and any additional query-only commands.
This module never discovers addresses, sends setting commands or resets errors.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pyvisa

from lab_control.application.config import ConnectionConfig, load_config
from lab_control.application.runtime import ApplicationRuntime
from lab_control.discovery.service import ConfiguredDiscoveryProvider
from lab_control.domain.instruments import DeviceError, jsonable, utc_now
from lab_control.instruments.base import ScpiAdapter
from lab_control.instruments.devices.definition import DeviceDefinition
from lab_control.instruments.registry import create_adapter
from lab_control.storage.repository import application_home
from lab_control.transport.base import Transport


class QueryOnlyTransport:
    def __init__(self, inner: Transport, allowed: set[str], log: Path):
        self.inner, self.allowed, self.log = inner, allowed, log
        self.resource = inner.resource

    def open(self) -> None:
        raise RuntimeError("The session owns opening this transport")

    def record(self, direction: str, value: str) -> None:
        with self.log.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps({"time": utc_now(), "direction": direction, "data": value}, ensure_ascii=False)
                + "\n"
            )

    def write(self, payload: bytes | str) -> None:
        if not isinstance(payload, str) or payload not in self.allowed:
            raise DeviceError("Read-only verification forbids this command", "unsupported")
        self.record("tx", payload)
        self.inner.write(payload)

    def read(self) -> bytes:
        try:
            response = self.inner.read()
        except Exception as exc:
            self.record("error", str(exc))
            raise
        self.record("rx", response.decode("ascii", errors="backslashreplace"))
        return response

    def query(self, payload: bytes | str) -> bytes:
        self.write(payload)
        return self.read()

    def close(self) -> None:
        self.inner.close()


def verify(
    definition: DeviceDefinition, config: ConnectionConfig, folder: Path, extra_queries: tuple[str, ...] = ()
) -> dict[str, Any]:
    profile = definition.profile
    if profile is None or definition.adapter_type is None:
        raise ValueError("This read-only verifier requires a SCPI model definition")
    allowed = {profile.identity_query, *extra_queries}
    allowed.update(
        spec.query.format(ch=ch)
        for spec in profile.commands.values()
        if spec.query
        for ch in profile.channels
    )
    # Error queries consume queue entries and are deliberately excluded here.
    allowed.discard(profile.error_query)
    for query in allowed:
        if "?" not in query.split(" ", 1)[0] or any(c in query for c in ";\r\n"):
            raise ValueError(f"Not an explicit read-only query: {query}")
    transcript = folder / "communication.jsonl"
    transcript.touch(exist_ok=False)
    report: dict[str, Any] = {
        "started_at": utc_now(),
        "adapter_id": config.adapter,
        "resource": config.resource,
        "expected_serial": config.expected_serial,
        "setting_writes": 0,
        "stages": {
            key: "not_tested"
            for key in (
                "scan",
                "identity",
                "connection",
                "parameter_read",
                "reconnection",
                "parameter_write",
                "restoration",
                "physical_output",
            )
        },
        "environment": {"python": platform.python_version(), "pyvisa": pyvisa.__version__},
        "source_sha256": {
            str(path.relative_to(definition.root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(definition.root.rglob("*.py"))
            if "tests" not in path.parts
        },
        "reconnections": [],
        "scope": "Read-only refactor verification; output state and error queue are not changed",
    }
    app: ApplicationRuntime | None = None
    stage = "scan"
    try:
        app = ApplicationRuntime((config,))  # Fixed per-user hardware ProcessLease.
        runtime = app

        def factory(selected: ConnectionConfig):
            if selected.adapter != config.adapter or selected.resource != config.resource:
                raise DeviceError("Only the explicit asset is allowed", "identity_conflict")
            adapter = create_adapter(selected, runtime.visa)
            if definition.adapter_type is None or not isinstance(adapter, definition.adapter_type):
                adapter.close()
                raise DeviceError("Model factory mismatch", "identity_conflict")
            adapter.transport = QueryOnlyTransport(adapter.transport, allowed, transcript)
            return adapter

        def probe(candidate):
            adapter = factory(candidate.config)
            try:
                identity = adapter.identify()
                if identity.serial != config.expected_serial:
                    raise DeviceError("Configured serial does not match", "identity_conflict")
                return identity, config.adapter
            finally:
                adapter.close()

        app.sessions.factory = factory
        app.discovery.providers = (ConfiguredDiscoveryProvider((config,)),)
        app.discovery.probe = probe
        report["scan"] = app.discovery.scan().result(timeout=90)
        failures = [item for item in report["scan"]["items"] if item.get("error")]
        if failures:
            raise DeviceError(str(failures), "connection_failed")
        report["stages"]["scan"] = "passed"
        stage = "connection"
        device_id = app.add(config)
        identity = app.connect(device_id, configure_acquisition=False).result(timeout=90)
        report["identity"] = jsonable(identity)
        report["stages"].update(identity="passed", connection="passed")
        if config.transport == "visa":
            report["environment"]["visa"] = str(app.visa.manager.visalib)

        def snapshot() -> dict[str, Any]:
            session = runtime.sessions.sessions[device_id]

            def read():
                adapter = session.adapter
                assert isinstance(adapter, ScpiAdapter)
                values = dict(adapter.read_status().values)
                for operation in ("waveform", "load", "phase"):
                    if operation in profile.commands and profile.commands[operation].capability.readable:
                        for ch in profile.channels:
                            values[f"{ch}.{operation}"] = adapter.read(operation, ch)
                return jsonable(values)

            return session.worker.submit(read).result(timeout=90)

        stage = "parameter_read"
        report["initial_snapshot"] = snapshot()
        report["capabilities"] = jsonable(app.sessions.capability_snapshot(device_id))
        report["stages"][stage] = "passed"
        stage = "reconnection"
        for attempt in range(3):
            app.sessions.disconnect(device_id)
            reconnect_identity = app.connect(device_id, configure_acquisition=False).result(timeout=90)
            actual = snapshot()
            report["reconnections"].append(
                {
                    "attempt": attempt + 1,
                    "identity": jsonable(reconnect_identity),
                    "snapshot": actual,
                    "identity_matches": reconnect_identity == identity,
                    "parameters_match": actual == report["initial_snapshot"],
                }
            )
            report["final_snapshot"] = actual
            if reconnect_identity != identity or actual != report["initial_snapshot"]:
                raise DeviceError(
                    "Identity or state changed during read-only verification", "readback_mismatch"
                )
        report["stages"][stage] = "passed"
        report["output_state_unchanged"] = True
    except Exception as exc:
        report["error"] = str(exc)
        report["error_code"] = getattr(exc, "code", type(exc).__name__)
        blocked = report["error_code"] in {
            "resource_busy",
            "timeout",
            "disconnected",
            "driver_missing",
            "connection_failed",
        }
        report["stages"][stage] = "blocked" if blocked else "failed"
        report["final_state"] = "No settings were sent; final state is unknown unless a snapshot is present"
    finally:
        if app:
            try:
                app.close()
                report["session_released"] = True
            except Exception as exc:
                report["close_error"] = str(exc)
        report["finished_at"] = utc_now()
        (folder / "results.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for name in ("initial_snapshot", "final_snapshot"):
            if name in report:
                (folder / f"{name}.json").write_text(
                    json.dumps(report[name], ensure_ascii=False, indent=2), encoding="utf-8"
                )
        rows = "\n".join(f"| {name} | {value} |" for name, value in report["stages"].items())
        (folder / "record.md").write_text(
            f"# {profile.models[0]} / {config.expected_serial} — 模块迁移后只读复核\n\n"
            f"时间：{report['started_at']} 至 {report['finished_at']}；资源：`{config.resource}`。\n\n"
            f"| 项目 | 结果 |\n| --- | --- |\n{rows}\n\n"
            f"错误：{report.get('error', '无')}。会话释放：{report.get('session_released', False)}。\n\n"
            "只访问明确配置的目标，经型号工厂和项目会话执行；遵守全局硬件锁。只发送显式允许的查询，"
            "未修改输出或参数，未清空/消费错误队列；恢复项不适用。输出状态以初始/最终快照为准，失联时不推断最终状态。"
            "参数写入及物理输出未在本批次验收；未排查网络。\n\n"
            "[结构化结果](results.json) · [原始通信](communication.jsonl)\n",
            encoding="utf-8",
        )
    return report


def asset_folder_name(model: str, serial: str, explicit: str | None = None) -> str:
    """Keep raw identity separate from a filesystem-safe asset directory name."""
    name = explicit or f"{model}-{re.sub(r'[^A-Za-z0-9_.-]', '_', serial)}"
    if name in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("Asset name must be a single safe directory name")
    return name


def main(
    definition: DeviceDefinition,
    extra_queries: tuple[str, ...] = (),
    record_summary: Callable[[Path], None] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--asset-name", help="Existing asset directory name, without path separators")
    args = parser.parse_args()
    configs = load_config(args.config)
    if (
        len(configs) != 1
        or configs[0].adapter != definition.adapter_id
        or not configs[0].expected_serial.strip()
        or configs[0].transport == "mock"
    ):
        parser.error("Require one exact real model configuration with an expected serial")
    config = configs[0]
    if definition.profile is None:
        parser.error("This read-only verifier requires a SCPI model definition")
    try:
        asset_name = asset_folder_name(definition.profile.models[0], config.expected_serial, args.asset_name)
    except ValueError as exc:
        parser.error(str(exc))
    folder = (
        application_home()
        / "hardware-tests"
        / asset_name
        / (datetime.now().strftime("%Y-%m-%d-%H%M%S-%f") + "-module-readonly")
    )
    folder.mkdir(parents=True, exist_ok=False)
    result = verify(definition, config, folder, extra_queries)
    if record_summary:
        record_summary(folder)
    from lab_control.storage.hardware_documents import refresh_after_batch

    refresh_after_batch(folder)
    print(folder, flush=True)
    print(json.dumps(result["stages"], ensure_ascii=False), flush=True)
    return int(bool(result.get("error") or result.get("close_error")))
