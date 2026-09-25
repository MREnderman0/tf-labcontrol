"""Read-only compound-query candidate. One message, one reply, no fallback/replay."""

from __future__ import annotations

import hashlib
import platform
import time
import traceback
from pathlib import Path
from typing import cast

import numpy as np

from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceError, jsonable, utc_now
from lab_control.instruments.registry import create_adapter
from lab_control.storage.repository import application_home

from ..adapter import CHANNEL_SETTINGS, GLOBAL_SETTINGS, WaveRunner9404MAdapter
from .benchmark import MetadataTransport
from .records import publish_report
from .validate_control import Verification, native_paths, save


def split_reply(raw: bytes, expected: list[bytes]) -> list[bytes]:
    """Exact scalar-by-scalar evidence also verifies order and complete reply count."""
    values = raw.strip(b"\r\n").split(b";")
    if [v.strip() for v in values] != [v.strip() for v in expected]:
        raise DeviceError(
            "Grouped scalar reply differs in count, order, header or value", "grouped_reply_mismatch"
        )
    return values


def verify(config, folder: Path):
    import pyvisa

    (folder / "communication.jsonl").touch(exist_ok=True)
    report = dict(
        adapter_id="lecroy.9404m",
        resource=config.resource,
        expected_serial=config.expected_serial,
        config=jsonable(config),
        started_at=utc_now(),
        scope="Read-only grouped startup scalar candidate",
        stages={"physical_output": "not_tested", "parameter_write": "not_tested"},
        operations=[],
        groups={},
        setting_writes=0,
        final_state="No settings written",
        environment={"python": platform.python_version(), "pyvisa": pyvisa.__version__},
        source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    )
    app = test = original = None
    stage = "connection"
    try:
        if config.adapter != "lecroy.9404m" or config.transport != "visa" or not config.expected_serial:
            raise DeviceError("Require selected VISA model and serial", "identity_required")
        report["physical_paths"] = native_paths(application_home())
        app = ApplicationRuntime((config,), root=folder / "runtime", hardware=True)

        def factory(candidate):
            assert candidate == config
            adapter = create_adapter(candidate, app.visa)
            assert isinstance(adapter, WaveRunner9404MAdapter)
            allowed = {"*IDN?"} | {
                s.query.format(ch=ch)
                for s in adapter.profile.commands.values()
                if s.query
                for ch in (1, 2, 3, 4)
            }
            adapter.transport = MetadataTransport(adapter.transport, folder, allowed)
            return adapter

        app.sessions.factory = factory
        key = app.add(config)
        report["identity"] = jsonable(app.connect(key, configure_acquisition=False).result(120))
        report["environment"]["visa"] = str(app.visa.manager.visalib)
        report["stages"].update(identity="passed", connection="passed")
        test = Verification(app, key, folder, report)
        original = test.snapshot()
        save(folder / "initial_snapshot.json", original)
        stage = "grouped_boundary_validation"
        # Native LeCroy mnemonics with explicit channel roots; no SCPI colon guessing.
        groups = [[test.adapter.profile.commands[op].query for op in GLOBAL_SETTINGS]] + [
            [test.adapter.profile.commands[op].query.format(ch=ch) for op in CHANNEL_SETTINGS]
            for ch in (1, 2, 3, 4)
        ]
        transport = cast(MetadataTransport, test.adapter.transport)
        references = [test.call(lambda commands=g: [transport.query(q) for q in commands]) for g in groups]

        def grouped(commands):
            payload = ";".join(commands)
            # Deliberate reviewed read-only candidate; bypass the single-command audit guard only here.
            if any("?" not in q or ";" in q or "\n" in q or "\r" in q for q in commands):
                raise DeviceError("Invalid read-only group", "unsupported")
            transport.last = payload
            transport.log(direction="tx", command=payload)
            transport.inner.write(payload)
            return transport.read()  # VISA END is the only message boundary, never a second read.

        for commands, expected in zip(groups, references, strict=True):
            raw = test.call(lambda g=commands: grouped(g))
            split_reply(raw, expected)
        report["stages"][stage] = "passed"
        stage = "grouped_performance"
        for round_index in range(3):
            for candidate in (False, True):
                samples = []
                for index in range(21):
                    started = time.perf_counter()
                    for commands, expected in zip(groups, references, strict=True):
                        if candidate:
                            split_reply(test.call(lambda g=commands: grouped(g)), expected)
                        else:
                            actual = test.call(lambda g=commands: [transport.query(q) for q in g])
                            if actual != expected:
                                raise DeviceError("Read-only baseline changed", "configuration_changed")
                    if index:
                        samples.append(time.perf_counter() - started)
                report["groups"][f"r{round_index + 1}-{'grouped' if candidate else 'scalar'}"] = {
                    "queries": len(groups) if candidate else sum(map(len, groups)),
                    "frames": 20,
                    "seconds": samples,
                    "median": float(np.median(samples)),
                    "p95": float(np.percentile(samples, 95)),
                    "maximum": max(samples),
                }
        report["stages"][stage] = "passed"
        report["qualified"] = all(
            report["groups"][f"r{r}-grouped"]["median"] <= report["groups"][f"r{r}-scalar"]["median"] * 0.8
            and report["groups"][f"r{r}-grouped"]["p95"] <= report["groups"][f"r{r}-scalar"]["p95"]
            for r in (1, 2, 3)
        )
        final = test.snapshot()
        report["final_snapshot"] = final
        save(folder / "final_snapshot.json", final)
        if final != original:
            raise DeviceError("Read-only final settings differ", "readback_mismatch")
        report["stages"]["parameter_read"] = "passed"
    except Exception as exc:
        report.update(error=str(exc), error_traceback=traceback.format_exc(), qualified=False)
        report["stages"][stage] = "failed"
        report["final_state"] = (
            "No settings written; candidate stopped and session closed without fallback queries"
        )
    finally:
        if app:
            try:
                app.close()
                report["session_released"] = True
            except Exception as exc:
                report["close_error"] = str(exc)
                report["qualified"] = False
        report["finished_at"] = utc_now()
        save(folder / "results.json", report)
        publish_report(folder)
        (folder / "record.md").write_text(
            "# Read-only grouped-query candidate\n\n"
            f"{report['final_state']}\n\n[Raw results](results.json) · [Summary](summary.json) · [Communication](communication.jsonl)\n",
            encoding="utf-8",
        )
        from lab_control.storage.hardware_documents import refresh_after_batch

        refresh_after_batch(folder)
    return report


if __name__ == "__main__":
    from .benchmark import main

    raise SystemExit(main(readonly=True))
