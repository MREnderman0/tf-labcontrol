"""Explicit multi-input thermistor plan; no I/O at import or argument parsing."""

from __future__ import annotations

from typing import Any

from lab_control.domain.instruments import DeviceError

from ..protocol import scan_channels
from .validate_control import save_json
from .verify import ThermistorTest

NOMINALS = (2252, 3000, 5000, 10000, 30000)


def validate_channel_nominals(values: dict[int, int]) -> None:
    if not values or any(
        type(ch) is not int
        or ch // 100 not in range(1, 6)
        or ch % 100 not in range(1, 33)
        or type(nominal) is not int
        or nominal not in NOMINALS
        for ch, nominal in values.items()
    ):
        raise ValueError("Specify MC3132 physical inputs and supported nominal resistances")


def parse_groups(groups: list[str]) -> dict[int, int]:
    """Example: ['401:416=5000', '417:419=10000']; never infer slot numbers."""
    result: dict[int, int] = {}
    for group in groups:
        selection, nominal = group.split("=")
        for ch in scan_channels(f"(@{selection})"):
            if ch in result:
                raise ValueError(f"Duplicate channel {ch}")
            result[ch] = int(nominal)
    validate_channel_nominals(result)
    return dict(sorted(result.items()))


class ThermistorBankTest(ThermistorTest):
    def __init__(self, app, device, folder, report, channel_nominals):
        validate_channel_nominals(channel_nominals)
        self.nominals = dict(sorted(channel_nominals.items()))
        super().__init__(app, device, folder, report, next(iter(self.nominals)))
        self.channels = tuple(self.nominals)
        self.scan = "(@" + ",".join(map(str, self.channels)) + ")"
        self.slots = tuple(sorted({ch // 100 * 100 for ch in self.channels}))
        self.original_temperature: dict[int, dict[str, Any]] = {}
        self.touched: set[int] = set()

    def apply(self, operation: str, value: Any, channel: int = 1) -> None:
        if channel != 1:
            if channel not in self.channels:
                raise ValueError("Test write outside the selected bank")
            # Include uncertain writes in restoration, even when apply raises.
            self.touched.add(channel)
        super().apply(operation, value, channel)

    def snapshot(self) -> dict[str, Any]:
        queries = dict(
            scan_list="ROUT:SCAN?",
            trigger_source="TRIG:SOUR?",
            trigger_count="TRIG:COUN?",
            trigger_interval="TRIG:TIM?",
            monitor_state="ROUT:MON:STAT?",
            monitor_list="ROUT:MON:CHAN?",
            dmm_state="INST:DMM?",
        )
        queries.update({f"format_{key}": f"FORM:READ:{key}?" for key in ("UNIT", "TIME", "CHAN", "ALAR")})
        for slot in self.slots:
            queries[f"relays_{slot}"] = f"ROUT:CLOS? (@{slot + 1}:{slot + 32})"
            for ch in range(slot + 1, slot + 33):
                prefix = "configuration" if ch in self.channels else "channel"
                queries[f"{prefix}_{ch}"] = f"CONF? (@{ch})"
        result = {key: self.q(query) for key, query in queries.items()}
        for ch in self.channels:
            result[f"function_{ch}"] = self.q(f"FUNC? (@{ch})")
            if result[f"function_{ch}"] == "TEMP":
                result[f"unit_{ch}"] = self.q(f"UNIT:TEMP? (@{ch})")
        return result

    def _backup_temperature(self, ch: int) -> dict[str, Any]:
        if ch not in self.original_temperature:
            sensor = self.q(f"TEMP:TRAN:TYPE? (@{ch})")
            if sensor not in {"TC", "THER"}:
                raise DeviceError(f"CH {ch}: original sensor is outside restoration plan")
            self.original_temperature[ch] = dict(sensor=sensor, unit=self.q(f"UNIT:TEMP? (@{ch})"))
            self.report["temperature_original"] = self.original_temperature
            self.checkpoint()
        return self.original_temperature[ch]

    def prepare(self) -> None:
        self.initial = self.snapshot()
        self.report["initial_snapshot"] = self.initial
        save_json(self.folder / "initial_snapshot.json", self.initial)
        initial = self.initial
        if (
            any(initial[f"function_{ch}"] not in {"VOLT", "RES", "TEMP"} for ch in self.channels)
            or not set(scan_channels(initial["scan_list"])) <= set(self.channels)
            or initial["monitor_state"] != "0"
            or initial["dmm_state"] != "1"
            or initial["trigger_source"] != "IMM"
            or float(initial["trigger_count"]) != 1
            or any(initial[f"format_{key}"] != "0" for key in ("UNIT", "TIME", "CHAN", "ALAR"))
            or any(float(v) for slot in self.slots for v in initial[f"relays_{slot}"].split(","))
        ):
            raise DeviceError("Initial state is outside the idle bank plan; no test writes", "not_configured")
        for ch in self.channels:
            if initial[f"function_{ch}"] == "TEMP":
                original = self._backup_temperature(ch)
                if original["sensor"] == "THER":
                    original["thermistor_type"] = int(float(self.q(f"TEMP:TRAN:THER:TYPE? (@{ch})")))
        points = int(float(self.q("DATA:POIN?")))
        self.report["memory_backup"] = dict(
            points=points,
            raw=self.q("FETC?") if points else "",
            restoration="READ? replaces reading memory; original values saved here, instrument memory cannot be restored",
        )
        self.checkpoint()
        if not self.q("SYST:ERR?").startswith(("0,", "+0,")):
            raise DeviceError("Pre-existing instrument error")
        for ch in self.channels:
            self.apply("function", "RES", ch)
            if self.q(f"RES:RANG:AUTO? (@{ch})") != "1":
                raise DeviceError(f"CH {ch}: resistance autorange off; leave latent range unchanged")
        self.apply("scan_list", self.scan)

    def configure_temperature(self, nominal: int) -> None:
        for ch, value in self.nominals.items():
            self.apply("function", "TEMP", ch)
            original = self._backup_temperature(ch)
            self.apply("temperature_sensor", "THER", ch)
            if "thermistor_type" not in original:
                original["thermistor_type"] = int(float(self.q(f"TEMP:TRAN:THER:TYPE? (@{ch})")))
                self.checkpoint()
            self.apply("thermistor_type", value, ch)
            self.apply("temperature_unit", "C", ch)
        self.apply("scan_list", self.scan)

    def restore(self) -> dict[str, Any]:
        errors: list[str] = []
        checks: list[dict[str, Any]] = []

        def restore_field(operation, expected, ch, query):
            try:
                actual = self.q(query)
                normalized = int(float(actual)) if isinstance(expected, int) else actual
                if normalized != expected:
                    self.apply(operation, expected, ch)
                actual = self.q(query)
                matched = (int(float(actual)) if isinstance(expected, int) else actual) == expected
                checks.append(
                    dict(channel=ch, operation=operation, expected=expected, actual=actual, matched=matched)
                )
                if not matched:
                    errors.append(f"CH {ch} {operation}: readback mismatch")
                return matched
            except Exception as exc:
                errors.append(f"CH {ch} {operation}: {exc}")
                return False

        for ch in sorted(self.touched):
            original = self.original_temperature.get(ch, {})
            if original:
                # A failure can occur in RES or halfway through the TEMP group.
                if restore_field("function", "TEMP", ch, f"FUNC? (@{ch})"):
                    if "thermistor_type" in original and restore_field(
                        "temperature_sensor", "THER", ch, f"TEMP:TRAN:TYPE? (@{ch})"
                    ):
                        restore_field(
                            "thermistor_type",
                            original["thermistor_type"],
                            ch,
                            f"TEMP:TRAN:THER:TYPE? (@{ch})",
                        )
                    restore_field("temperature_unit", original["unit"], ch, f"UNIT:TEMP? (@{ch})")
                    restore_field("temperature_sensor", original["sensor"], ch, f"TEMP:TRAN:TYPE? (@{ch})")
            restore_field("function", self.initial[f"function_{ch}"], ch, f"FUNC? (@{ch})")
        restore_field("scan_list", self.initial["scan_list"], 1, "ROUT:SCAN?")
        try:
            final = self.snapshot()
            self.report["final_state"] = final
            differences = {
                key: dict(before=value, after=final.get(key))
                for key, value in self.initial.items()
                if final.get(key) != value
            }
            error_queue = self.q("SYST:ERR?")
            if not error_queue.startswith(("0,", "+0,")):
                errors.append(error_queue)
        except Exception as exc:
            errors.append(f"Final snapshot: {exc}")
            differences, error_queue = {}, "unknown"
        return dict(
            state="failed" if errors or differences else "passed",
            checks=checks,
            differences=differences,
            errors=errors,
            error_queue=error_queue,
        )
