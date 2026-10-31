"""Explicit MC3132 thermistor acceptance, using the shared locks and recorder."""

from __future__ import annotations

import argparse
import hashlib
import platform
import subprocess
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyvisa

from lab_control.application.runtime import ApplicationRuntime
from lab_control.discovery.types import DiscoveryCandidate
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError, jsonable, utc_now
from lab_control.instruments.common.hardware_paths import verify_runtime_paths
from lab_control.storage.hardware_documents import refresh_after_batch
from lab_control.storage.recording import read_frames
from lab_control.storage.repository import application_home
from lab_control.storage.runtime import hardware_lock_home

from ..adapter import M300Adapter
from ..protocol import token
from .records import publish_report
from .validate_control import RecordingTransport, save_json


class ThermistorTest:
    def __init__(self, app, device, folder: Path, report: dict, channel: int):
        self.app, self.device, self.folder, self.report, self.channel = app, device, folder, report, channel
        self.channels = (channel,)
        self.latent: dict[str, Any] = {}
        self.initial: dict[str, Any] = {}
        self.modified = False

    def q(self, query: str) -> str:
        session = self.app.sessions.sessions[self.device]
        return token(session.worker.submit(lambda: session.adapter.transport.query(query)).result())

    def checkpoint(self) -> None:
        save_json(self.folder / "progress.json", self.report)

    def apply(self, operation: str, value: Any, channel: int = 1) -> None:
        self.modified = True
        entry = dict(time=utc_now(), operation=operation, value=value, channel=channel)
        self.report["operations"].append(entry)
        self.checkpoint()
        result = self.app.sessions.submit(
            self.device, "apply", DeviceCommand(operation, {"value": value}, channel, True)
        ).result()
        entry["result"] = jsonable(result)
        self.checkpoint()
        if result.outcome != "success":
            raise DeviceError(f"{operation}: {result.outcome}: {result.error}", "readback_mismatch")

    def snapshot(self) -> dict[str, str]:
        ch = self.channel
        slot = ch // 100 * 100
        queries = dict(
            function=f"FUNC? (@{ch})",
            configuration=f"CONF? (@{ch})",
            scan_list="ROUT:SCAN?",
            trigger_source="TRIG:SOUR?",
            trigger_count="TRIG:COUN?",
            trigger_interval="TRIG:TIM?",
            monitor_state="ROUT:MON:STAT?",
            monitor_list="ROUT:MON:CHAN?",
            dmm_state="INST:DMM?",
            relays=f"ROUT:CLOS? (@{slot + 1}:{slot + 32})",
        )
        queries.update({f"format_{key}": f"FORM:READ:{key}?" for key in ("UNIT", "TIME", "CHAN", "ALAR")})
        queries.update(
            {f"channel_{other}": f"CONF? (@{other})" for other in range(slot + 1, slot + 33) if other != ch}
        )
        return {key: self.q(query) for key, query in queries.items()}

    def capture(self, label: str, count: int = 3) -> None:
        for index in range(count):
            deadline = time.monotonic() + 30
            while True:
                try:
                    frame = self.app.sessions.submit(
                        self.device, "acquire", AcquisitionRequest("scalar", self.channels, confirmed=True)
                    ).result()
                    break
                except DeviceError as exc:
                    if exc.code != "configuration_changed" or time.monotonic() >= deadline:
                        raise
                    self.report.setdefault("discarded_frames", []).append(
                        dict(time=utc_now(), label=label, reason=str(exc))
                    )
                    self.checkpoint()
                    time.sleep(0.1)
            if tuple(m.channel for m in frame.measurements) != self.channels or any(
                len(m.values) != 1 or m.quality != "good" for m in frame.measurements
            ):
                raise DeviceError("Invalid input frame", "acquisition_failed")
            raw = jsonable(frame)
            save_json(self.folder / f"{label}-{index + 1:02}.json", raw)
            self.report["samples"].append(dict(label=label, frame=raw))
            self.checkpoint()

    def configure_temperature(self, nominal: int) -> None:
        ch = self.channel
        self.apply("function", "TEMP", ch)
        if not self.latent:
            self.latent.update(sensor=self.q(f"TEMP:TRAN:TYPE? (@{ch})"), unit=self.q(f"UNIT:TEMP? (@{ch})"))
            self.report["temperature_original"] = self.latent
            self.checkpoint()
        self.apply("temperature_sensor", "THER", ch)
        if "thermistor_type" not in self.latent:
            self.latent["thermistor_type"] = int(float(self.q(f"TEMP:TRAN:THER:TYPE? (@{ch})")))
            self.checkpoint()
        self.apply("thermistor_type", nominal, ch)
        self.apply("temperature_unit", "C", ch)
        self.apply("scan_list", f"(@{ch})")

    def set_temperature_unit(self, unit: str) -> None:
        for channel in self.channels:
            self.apply("temperature_unit", unit, channel)

    def prepare(self) -> None:
        _prepare_single(self, self.report, self.folder, self.channel)

    def restore(self) -> dict[str, Any]:
        """Observe each field first; do not replay an uncertain test command."""
        errors, checks = [], []
        ch = self.channel
        fields = []
        if "thermistor_type" in self.latent:
            fields.append(
                ("thermistor_type", self.latent["thermistor_type"], ch, f"TEMP:TRAN:THER:TYPE? (@{ch})")
            )
        for name, operation, query in (
            ("unit", "temperature_unit", "UNIT:TEMP"),
            ("sensor", "temperature_sensor", "TEMP:TRAN:TYPE"),
        ):
            if name in self.latent:
                fields.append((operation, self.latent[name], ch, f"{query}? (@{ch})"))
        fields.extend(
            [
                ("scan_list", self.initial["scan_list"], 1, "ROUT:SCAN?"),
                ("function", self.initial["function"], ch, f"FUNC? (@{ch})"),
            ]
        )
        for operation, expected, channel, query in fields:
            try:
                actual = self.q(query)
                if (int(float(actual)) if isinstance(expected, int) else actual) != expected:
                    self.apply(operation, expected, channel)
                actual = self.q(query)
                matched = (int(float(actual)) if isinstance(expected, int) else actual) == expected
                checks.append(dict(operation=operation, expected=expected, actual=actual, matched=matched))
                if not matched:
                    errors.append(f"{operation}: readback mismatch")
            except Exception as exc:
                errors.append(f"{operation}: {exc}")
        final = self.snapshot()
        differences = {
            key: {"before": value, "after": final.get(key)}
            for key, value in self.initial.items()
            if final.get(key) != value
        }
        error_queue = self.q("SYST:ERR?")
        self.report["final_state"] = final
        return dict(
            state="passed"
            if not differences and not errors and error_queue.startswith(("0,", "+0,"))
            else "failed",
            checks=checks,
            differences=differences,
            errors=errors,
            error_queue=error_queue,
        )

    def monitor(self, duration: float) -> None:
        app, device = self.app, self.device
        app.monitoring.start(
            device,
            (AcquisitionRequest("scalar", self.channels, confirmed=True),),
            interval=0.5,
            record_interval=None,
            recording=True,
        )
        recorder = app.monitoring.tasks[device].recorder
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            state = app.monitoring.snapshots()[device]
            if state.get("error"):
                raise DeviceError(state["error"], "acquisition_failed")
            time.sleep(0.2)
        app.monitoring.stop(device).result(timeout=30)
        if recorder is None:
            raise DeviceError("No recorder was started")
        recorder.finished.result(timeout=30)
        state = recorder.snapshot()
        frames = [frame for path in recorder.segments.paths for frame in read_frames(Path(path))]
        save_json(self.folder / "recorded-frames.json", frames)
        if (
            state["error"]
            or state["csv_error"]
            or not frames
            or not (len(frames) == state["received"] == state["committed"] == state["converted"])
        ):
            raise DeviceError("Recording completeness check failed: " + str(state))
        if any(
            tuple(m["channel"] for m in frame["measurements"]) != self.channels
            or any(m["unit"] != "°C" for m in frame["measurements"])
            for frame in frames
        ):
            raise DeviceError("Recording channel/unit mismatch")
        self.report["stages"]["monitoring"] = dict(state="passed", frames=len(frames), duration=duration)
        self.report["stages"]["recording"] = dict(
            state="passed", **{k: v for k, v in state.items() if k != "state"}
        )
        unlocked = not app.sessions.sessions[device].controls_locked
        if not unlocked:
            raise DeviceError("Parameters remain locked after recorder stopped", "task_busy")
        self.report["stages"]["stop"] = dict(state="passed", controls_unlocked=True)


def verify_readonly(config, folder, *, duration=0, root=None, input_condition="unspecified"):
    return verify(config, Path(folder), root=root, input_condition=input_condition)


def verify(
    config: ConnectionConfig,
    folder: Path,
    *,
    root=None,
    channel: int | None = None,
    channel_nominals: dict[int, int] | None = None,
    nominal=10000,
    duration=8.0,
    leave_configured=False,
    input_condition="thermistor; curve unverified",
):
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / "results.json").exists():
        raise ValueError("Do not overwrite an existing batch")
    report: dict[str, Any] = dict(
        schema_version=1,
        adapter_id="rigol.m300",
        model="M300",
        expected_serial=config.expected_serial,
        resource=config.resource,
        interface="LAN" if config.resource.startswith("TCPIP") else "USB",
        started_at=utc_now(),
        operator="user-authorized project hardware entry",
        scope=dict(
            channel=channel,
            channel_nominals=channel_nominals,
            nominal_ohm=nominal,
            duration=duration,
            leave_configured=leave_configured,
            input_condition=input_condition,
        ),
        environment=dict(
            python=platform.python_version(), pyvisa=pyvisa.__version__, platform=platform.platform()
        ),
        stages={
            key: {"state": "not_tested"}
            for key in (
                "enumeration",
                "identity",
                "compatibility_connection",
                "parameter_read",
                "parameter_control",
                "resistance",
                "temperature",
                "monitoring",
                "recording",
                "stop",
                "recovery",
                "reconnect",
                "commissioning",
                "physical_output",
            )
        },
        operations=[],
        samples=[],
    )
    try:
        report["source_version"] = dict(
            commit=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            dirty=subprocess.check_output(["git", "status", "--short"], text=True),
        )
    except (OSError, subprocess.CalledProcessError):
        report["source_version"] = "unavailable; model source hashes retained"
    report["model_source_hashes"] = {
        str(p.relative_to(Path(__file__).parents[1])): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in Path(__file__).parents[1].rglob("*.py")
    }
    app, device = None, ""
    test: ThermistorTest | None = None
    stage = "identity"
    try:
        if config.adapter != "rigol.m300" or not config.expected_serial:
            raise ValueError("Explicit M300 adapter and expected serial required")
        if channel_nominals is not None:
            from .thermistor_bank import validate_channel_nominals

            validate_channel_nominals(channel_nominals)
            if channel is not None:
                raise ValueError("Choose either one channel or a channel bank")
            if not 1 <= duration <= 60:
                raise ValueError("Choose a bounded 1..60 second monitoring test")
        if channel is not None and (
            nominal not in (2252, 3000, 5000, 10000, 30000) or not 1 <= duration <= 60
        ):
            raise ValueError("Choose a supported thermistor and a bounded 1..60 second monitoring test")
        home = Path(root) if root else application_home()
        report["paths"] = verify_runtime_paths(home, hardware_lock_home())
        app = ApplicationRuntime((), root=home, hardware=True)
        report["environment"]["visa_library"] = str(app.visa.manager.visalib)
        factory = app.sessions.factory

        def logged(selected):
            if (selected.adapter, selected.resource, selected.expected_serial) != (
                config.adapter,
                config.resource,
                config.expected_serial,
            ):
                raise DeviceError("Only the selected device is authorized", "identity_conflict")
            adapter = factory(selected)
            assert isinstance(adapter, M300Adapter)
            adapter.transport = RecordingTransport(adapter.transport, folder / "communication.jsonl")
            return adapter

        app.sessions.factory = logged
        try:
            advertisements = (
                app.visa.discover_lan(timeout=2)
                if config.resource.startswith("TCPIP")
                else app.visa.enumerate()
            )
            report["stages"]["enumeration"] = dict(
                state="passed" if config.resource in advertisements else "not_tested",
                advertisements=advertisements,
            )
        except Exception as exc:
            report["stages"]["enumeration"] = dict(state="blocked", reason=str(exc))
        owner = "m300-acceptance-identity"
        app.leases.acquire(owner, (config.resource, "discovery:unknown_alias"))
        try:
            probe = logged(config)
            try:
                identity = probe.identify()
                if identity.serial != config.expected_serial:
                    raise DeviceError("Selected serial differs", "identity_conflict")
            finally:
                probe.close()
        finally:
            app.leases.release(owner)
        report["identity"] = jsonable(identity)
        report["stages"]["identity"] = dict(state="passed")
        stage = "compatibility_connection"
        row = app.inventory.merge(
            DiscoveryCandidate(
                config, "lan" if config.resource.startswith("TCPIP") else "visa", safe_scpi=True
            ),
            folder.name,
            identity,
            "rigol.m300",
            reason="identified",
        )
        device = row["id"]
        app.inventory.select_resource(device, config.resource, follow_address=True)
        app.connect(device, configure_acquisition=False).result()
        report["stages"][stage] = dict(state="passed")
        report["initial_status"] = jsonable(app.sessions.submit(device, "status").result())
        report["stages"]["parameter_read"] = dict(
            state="passed" if not report["initial_status"]["errors"] else "failed"
        )
        if channel is not None or channel_nominals is not None:
            if channel_nominals is not None:
                from .thermistor_bank import ThermistorBankTest

                test = ThermistorBankTest(app, device, folder, report, channel_nominals)
            else:
                assert channel is not None
                test = ThermistorTest(app, device, folder, report, channel)
            connected_adapter = app.sessions.sessions[device].adapter
            assert isinstance(connected_adapter, M300Adapter)
            if any(ch == 1 or ch not in connected_adapter.capabilities().channels for ch in test.channels):
                raise ValueError("Channel must be an identified MC3132 input")
            stage = "parameter_control"
            test.prepare()
            stage = "resistance"
            test.capture("resistance")
            report["stages"][stage] = dict(state="passed")
            test.configure_temperature(nominal)
            stage = "temperature"
            test.capture("temperature")
            test.set_temperature_unit("F")
            test.capture("fahrenheit", 1)
            test.set_temperature_unit("C")
            report["stages"][stage] = dict(state="passed", assumed_curve=True)
            current = test.snapshot()
            unchanged = all(current[k] == test.initial[k] for k in test.initial if k.startswith("channel_"))
            if not unchanged:
                raise DeviceError("Other channel configuration changed")
            report["stages"]["parameter_control"] = dict(state="passed", unselected_channels_unchanged=True)
            stage = "monitoring"
            test.monitor(duration)
    except (Exception, KeyboardInterrupt) as exc:
        report["error"], report["traceback"] = str(exc) or type(exc).__name__, traceback.format_exc()
        blocked = isinstance(exc, DeviceError) and exc.code in {
            "process_busy",
            "resource_busy",
            "driver_missing",
            "not_configured",
        }
        report["stages"][stage] = dict(state="blocked" if blocked else "failed", reason=str(exc))
    finally:
        if app and device:
            finalization_stage = "recovery"
            try:
                try:
                    app.monitoring.stop(device).result(timeout=30)
                except Exception as exc:
                    report["stop_error"] = str(exc)
                    report["stages"]["stop"] = dict(state="failed", reason=str(exc))
                if test and test.modified:
                    report["stages"]["recovery"] = test.restore()
                finalization_stage = "reconnect"
                app.sessions.disconnect(device)
                identity = app.connect(device, configure_acquisition=False).result()
                again = (
                    test.snapshot()
                    if test and test.initial
                    else jsonable(app.sessions.submit(device, "status").result())
                )
                matched = (
                    again == report.get("final_state", report.get("initial_status"))
                    if test and test.initial
                    else not again["errors"]
                )
                report["reconnected_snapshot"] = again
                report["stages"]["reconnect"] = dict(
                    state="passed" if matched else "failed",
                    identity=jsonable(identity),
                    settings_match=matched,
                )
                if (
                    leave_configured
                    and test
                    and not report.get("error")
                    and not report.get("stop_error")
                    and matched
                    and report["stages"]["recovery"]["state"] == "passed"
                ):
                    finalization_stage = "commissioning"
                    test.configure_temperature(nominal)
                    test.capture("commissioned", 1)
                    report["final_state"] = test.snapshot()
                    report["stages"]["commissioning"] = dict(
                        state="passed",
                        channels=test.channels,
                        channel_nominals=channel_nominals or {test.channel: nominal},
                        unit="°C",
                        scan_list=test.q("ROUT:SCAN?"),
                        note="After verified restoration, user-requested thermistor configuration deliberately retained; no scan running",
                    )
            except Exception as exc:
                report["finalization_error"] = str(exc)
                report["final_state_warning"] = "Final settings not fully verified; inspect raw transcript"
                report["stages"][finalization_stage] = dict(state="failed", reason=str(exc))
                if finalization_stage == "commissioning" and test:
                    try:
                        recovery = test.restore()
                        report["stages"]["commissioning_recovery"] = recovery
                        if recovery["state"] == "passed":
                            report["final_state_warning"] = (
                                "Commissioning failed; original settings restored and verified"
                            )
                    except Exception as recovery_exc:
                        report["stages"]["commissioning_recovery"] = dict(
                            state="failed", reason=str(recovery_exc)
                        )
        if app:
            try:
                app.close()
            except Exception as exc:
                report["close_error"] = str(exc)
        report["finished_at"] = utc_now()
        report["stages"]["physical_output"] = dict(
            state="not_tested",
            reason="No independent resistance/temperature reference; nominal thermistor curve is an assumption, not accuracy acceptance",
        )
        save_json(folder / "results.json", report)
        (folder / "record.md").write_text(
            "# M300 输入与软件接入验证\n\n10 kΩ 等标称阻值仅为换算假设，未校验传感器曲线和测温精度。新扫描会覆盖仪器内部读数；原值备份在 results.json。\n\n"
            + "\n".join(
                f"- {key}: **{value['state']}** {value.get('reason', '')}"
                for key, value in report["stages"].items()
            )
            + "\n\n参数、逐帧数据、恢复与最终配置见 results.json；原始通信见 communication.jsonl。\n",
            encoding="utf-8",
        )
        publish_report(folder)
        refresh_after_batch(folder)
    return report


def _prepare_single(test, report, folder, channel):
    test.initial = test.snapshot()
    report["initial_snapshot"] = test.initial
    save_json(folder / "initial_snapshot.json", test.initial)
    initial = test.initial
    if (
        initial["function"] not in {"VOLT", "RES", "TEMP"}
        or initial["scan_list"] not in {"(@)", f"(@{channel})"}
        or initial["monitor_state"] != "0"
        or initial["trigger_source"] != "IMM"
        or float(initial["trigger_count"]) != 1
        or any(initial[f"format_{key}"] != "0" for key in ("UNIT", "TIME", "CHAN", "ALAR"))
        or any(float(v) for v in initial["relays"].split(","))
    ):
        raise DeviceError(
            "Initial state differs from idle single-scan plan; no test writes", "not_configured"
        )
    if initial["function"] == "TEMP" and test.q(f"TEMP:TRAN:TYPE? (@{channel})") not in {
        "TC",
        "THER",
    }:
        raise DeviceError("Original temperature sensor is outside this restoration plan")
    if initial["function"] == "TEMP":
        test.latent.update(
            sensor=test.q(f"TEMP:TRAN:TYPE? (@{channel})"), unit=test.q(f"UNIT:TEMP? (@{channel})")
        )
        if test.latent["sensor"] == "THER":
            test.latent["thermistor_type"] = int(float(test.q(f"TEMP:TRAN:THER:TYPE? (@{channel})")))
        report["temperature_original"] = test.latent
    points = int(float(test.q("DATA:POIN?")))
    report["memory_backup"] = dict(
        points=points,
        raw=test.q("FETC?") if points else "",
        restoration="READ? replaces reading memory; original values saved here, instrument memory cannot be restored",
    )
    if not test.q("SYST:ERR?").startswith(("0,", "+0,")):
        raise DeviceError("Pre-existing instrument error")
    test.apply("function", "RES", channel)
    if test.q(f"RES:RANG:AUTO? (@{channel})") != "1":
        raise DeviceError("Resistance autorange is off; leave latent range unchanged")
    test.apply("scan_list", f"(@{channel})")


def main(argv=None, *, readonly=False) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resource", required=True)
    parser.add_argument("--expected-serial", required=True)
    parser.add_argument("--home", type=Path, default=application_home())
    if not readonly:
        selection = parser.add_mutually_exclusive_group(required=True)
        selection.add_argument("--channel", type=int)
        selection.add_argument("--group", action="append", help="Physical channel range=nominal ohms")
        parser.add_argument("--nominal", type=int, choices=(2252, 3000, 5000, 10000, 30000), default=10000)
        parser.add_argument("--duration", type=float, default=8)
        parser.add_argument("--leave-configured", action="store_true")
        parser.add_argument("--input-condition", default="thermistor; curve unverified")
    args = parser.parse_args(argv)
    channel_nominals = None
    if not readonly and args.group:
        from .thermistor_bank import parse_groups

        try:
            channel_nominals = parse_groups(args.group)
        except (ValueError, DeviceError) as exc:
            parser.error(str(exc))
    folder = (
        args.home
        / "hardware-tests"
        / f"RIGOL-M300-{args.expected_serial}"
        / (
            datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
            + ("-readonly" if readonly else "-thermistor-integration")
        )
    )
    report = verify(
        ConnectionConfig(
            args.resource, "rigol.m300", "visa", expected_serial=args.expected_serial, timeout=15
        ),
        folder,
        root=args.home,
        **(
            {}
            if readonly
            else dict(
                channel=args.channel,
                channel_nominals=channel_nominals,
                nominal=args.nominal,
                duration=args.duration,
                leave_configured=args.leave_configured,
                input_condition=args.input_condition,
            )
        ),
    )
    print(folder)
    return (
        int(
            bool(
                report.get("error")
                or report.get("finalization_error")
                or report.get("close_error")
                or any(s["state"] == "failed" for s in report["stages"].values())
            )
        )
        * 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
