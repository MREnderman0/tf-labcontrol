"""SMA100B SCPI protocol and verified dynamic range queries."""

from __future__ import annotations

import csv
import re
from dataclasses import replace
from typing import Any

from lab_control.domain.instruments import (
    ApplyResult,
    Capability,
    CapabilitySet,
    DeviceCommand,
    DeviceError,
    Parameter,
    Unsupported,
    ValidationError,
    utc_now,
)
from lab_control.instruments.base import CommandSpec, Profile, ScpiAdapter, command, enum, number, parameter
from lab_control.transport.base import block_payload

BOOL = Parameter("boolean")
SMA_SOURCE = "https://scdn.rohde-schwarz.com/ur/pws/dl_downloads/pdm/cl_manuals/user_manual/1178_3834_01/SMA100B_UserManual_en_14.pdf"


def modulation_source(raw: bytes) -> str:
    value = raw.decode("ascii").strip().upper()
    return {"LF1": "INT", "INTERNAL": "INT", "EXT1": "EXT", "EXTERNAL": "EXT", "NOISE": "NOIS"}.get(
        value, value
    )


def sine_shape(raw: bytes) -> str:
    value = raw.decode("ascii").strip().upper()
    return {"SINE": "SIN", "SQUARE": "SQU", "TRIANGLE": "TRI", "TRAPEZE": "TRAP", "PULSE": "PULS"}.get(
        value, value
    )


def quote_name(value: str) -> str:
    if not value or any(char in value for char in "'\";\r\n"):
        raise ValidationError("Unsafe list filename")
    return "'" + value + "'"


def sma100b() -> Profile:
    commands = {}
    for name, path, param in (
        ("frequency", "SOUR:FREQ", parameter("Hz", 8000, 3000000000.0)),
        ("power", "SOUR:POW", parameter("dBm", -120, 18)),
        ("frequency_mode", "SOUR:FREQ:MODE", enum("CW", "SWE", "LIST")),
        ("sweep_start", "SOUR:FREQ:STAR", parameter("Hz", 8000, 3000000000.0)),
        ("sweep_stop", "SOUR:FREQ:STOP", parameter("Hz", 8000, 3000000000.0)),
        ("sweep_spacing", "SOUR:SWE:FREQ:SPAC", enum("LIN", "LOG")),
        ("sweep_dwell", "SOUR:SWE:FREQ:DWEL", parameter("s", 0.001, 100)),
        ("reference_clock", "SOUR:ROSC:SOUR", enum("INT", "EXT")),
    ):
        commands[name] = command(name, path, param, scoped=False)
    commands["output"] = command("output", "OUTP", BOOL, scoped=False, confirm=True)
    for modulation in ("AM", "FM", "PM"):
        key = modulation.lower()
        commands[key + "_enabled"] = command(
            key + "_enabled", "SOUR:" + modulation + "1:STAT", BOOL, scoped=False, confirm=True
        )
        commands[key + "_source"] = replace(
            command(
                key + "_source",
                "SOUR:" + modulation + "1:SOUR",
                enum("INT", "EXT", "LF2", "EXT2", "NOIS"),
                scoped=False,
            ),
            decode=modulation_source,
        )
        commands[key + "_frequency"] = command(
            key + "_frequency", "SOUR:LFO1:FREQ", parameter("Hz", 0.1, 1e6), scoped=False
        )
        commands[key + "_shape"] = replace(
            command(
                key + "_shape", "SOUR:LFO1:SHAP", enum("SIN", "SQU", "TRI", "TRAP", "PULS"), scoped=False
            ),
            decode=sine_shape,
        )
    commands["am_depth"] = command("am_depth", "SOUR:AM1:DEPT", parameter("%", 0, 100), scoped=False)
    commands["fm_deviation"] = command(
        "fm_deviation", "SOUR:FM1:DEV", parameter("Hz", 0, 1000000.0), scoped=False
    )
    commands["pm_deviation"] = command("pm_deviation", "SOUR:PM1:DEV", parameter("rad", 0, 20), scoped=False)
    commands["pulse_enabled"] = command(
        "pulse_enabled", "SOUR:PULM:STAT", BOOL, scoped=False, confirm=True, option="pulse_modulation"
    )
    for name, path, param in (
        ("list_mode", "SOUR:LIST:MODE", enum("AUTO", "STEP")),
        ("list_trigger_source", "SOUR:LIST:TRIG:SOUR", enum("AUTO", "SING", "EXT")),
        ("list_dwell", "SOUR:LIST:DWEL", parameter("s", 0.001, 100)),
    ):
        commands[name] = command(name, path, param, scoped=False)
    commands["list_trigger"] = command(
        "list_trigger", "SOUR:LIST:TRIG:EXEC", read=False, scoped=False, confirm=True
    )
    commands["upload_list"] = CommandSpec(
        Capability(
            "upload_list",
            {
                "name": Parameter("text"),
                "frequencies": Parameter("array", minimum=300000, maximum=3000000000.0),
                "powers": Parameter("array", minimum=-120, maximum=18),
            },
            writable=True,
            requires_confirmation=True,
            section="15.18.8 LIST subsystem",
        )
    )
    for name, path, param in (
        ("sweep_mode", "SOUR:SWE:FREQ:MODE", enum("AUTO", "MAN", "STEP")),
        ("sweep_step_linear", "SOUR:SWE:FREQ:STEP:LIN", parameter("Hz", 0.01, 3e9)),
        ("sweep_step_log", "SOUR:SWE:FREQ:STEP:LOG", parameter("%", 0.01, 100)),
    ):
        commands[name] = command(name, path, param, scoped=False)
    return Profile(
        "rs.sma100b",
        ("Rohde&Schwarz", "Rohde-Schwarz"),
        ("SMA100B",),
        "signal_generator",
        (1,),
        SMA_SOURCE,
        "1178.3834.01 revision 14",
        commands,
        ("frequency", "power", "output"),
    )


class Sma100bAdapter(ScpiAdapter):
    @property
    def rf_off_list_mode_block_reason(self) -> str:
        if self.identity and self.identity.firmware == "4.60.112.29 SP6":
            return "Firmware 4.60.112.29 SP6 rejected LIST mode with RF OFF (-221); RF remains OFF"
        return ""

    def capabilities(self) -> CapabilitySet:
        base = super().capabilities()
        options = set(self.detected_options if self.identity else self.config.options)
        operations = []
        for cap in base.operations:
            if cap.name.startswith(("am_", "fm_", "pm_")):
                cap = replace(
                    cap,
                    available="SMAB-K720" in options,
                    reason="" if "SMAB-K720" in options else "missing option SMAB-K720",
                )
                if cap.name.endswith("_shape") and "SMAB-K24" not in options:
                    cap = replace(cap, writable=False, parameters={"value": enum("SIN")})
            if cap.name == "pulse_enabled":
                cap = replace(
                    cap,
                    available="SMAB-K22" in options,
                    reason="" if "SMAB-K22" in options else "missing option SMAB-K22",
                )
            if cap.name == "upload_list":
                frequency = self.dynamic_parameters.get(
                    "frequency", self.profile.commands["frequency"].capability.parameters["value"]
                )
                power = self.dynamic_parameters.get(
                    "power", self.profile.commands["power"].capability.parameters["value"]
                )
                params = dict(cap.parameters)
                for name, scalar, limit_key in (
                    ("frequencies", frequency, "frequency"),
                    ("powers", power, "power"),
                ):
                    maximum = min(
                        scalar.maximum or float("inf"), self.config.limits.get(limit_key, float("inf"))
                    )
                    minimum = max(300000, scalar.minimum or 0) if name == "frequencies" else scalar.minimum
                    params[name] = replace(
                        params[name], minimum=minimum, maximum=maximum, range_source=scalar.range_source
                    )
                cap = replace(
                    cap,
                    parameters=params,
                    available=not bool(self.capability_error),
                    reason="实机范围已过期：" + self.capability_error if self.capability_error else "",
                )
            operations.append(cap)
        return replace(base, operations=tuple(operations))

    def refresh_capabilities(self) -> None:
        """Only verified, query-only model commands. Publish complete snapshots atomically."""
        names = ("frequency", "sweep_start", "sweep_stop", "power")
        try:
            options = tuple(
                (s.strip() for s in self.transport.query("*OPT?").decode("ascii").split(",") if s.strip())
            )
            updated = {}
            for name in names:
                spec = self.profile.commands[name]
                low = number(self.transport.query(spec.query + " MIN"))
                high = number(self.transport.query(spec.query + " MAX"))
                if low > high:
                    raise ValidationError(f"{name}: instrument minimum exceeds maximum")
                updated[name] = replace(
                    spec.capability.parameters["value"], minimum=low, maximum=high, range_source="instrument"
                )
            details = {
                "power_offset_db": number(self.transport.query("SOUR:POW:OFFS?")),
                "rf_output_limit_dbm": number(self.transport.query("SOUR:POW:LIM?")),
                "scope": "Settable levels; not guaranteed RF power. RF limit is unchanged.",
                "read_at": utc_now(),
            }
        except Exception as exc:
            self.capability_error = str(exc)
            if not self.dynamic_parameters:
                self.dynamic_parameters = {
                    name: replace(
                        self.profile.commands[name].capability.parameters["value"], range_source="unavailable"
                    )
                    for name in names
                }
            raise
        self.dynamic_parameters, self.detected_options = (updated, options)
        self.capability_details, self.capability_error = (details, "")

    def validate_command(self, cmd: DeviceCommand) -> tuple[CommandSpec, dict[str, Any]]:
        if self.identity is not None:
            self.refresh_capabilities()
        spec, values = super().validate_command(cmd)
        if "name" in values:
            if not re.fullmatch("[A-Za-z][A-Za-z0-9_]{0,23}", values["name"]):
                raise ValidationError("Name: ASCII letters/digits/underscore, max 24")
        if cmd.operation == "upload_list" and (
            not 1 <= len(values["frequencies"]) == len(values["powers"]) <= 10000
        ):
            raise ValidationError("Frequency/power lists must have the same 1..10000 length")
        return (spec, values)

    def preflight(self, cmd: DeviceCommand, parameters: dict[str, Any]) -> None:
        super().preflight(cmd, parameters)
        if (
            cmd.operation == "frequency_mode"
            and parameters["value"] == "LIST"
            and self.rf_off_list_mode_block_reason
            and not self.read("output")
        ):
            raise Unsupported(self.rf_off_list_mode_block_reason)
        mode = cmd.operation.split("_", 1)[0].upper()
        if cmd.operation == "am_depth" and self.transport.query("SOUR:AM:TYPE?").strip().upper() != b"LIN":
            raise ValidationError("Percent AM depth requires linear AM type")
        if cmd.operation in {"am_depth", "fm_deviation", "pm_deviation"}:
            if self.transport.query(f"SOUR:{mode}:DEV:MODE?").strip().upper() != b"UNC":
                raise ValidationError("Coupled modulation paths must be explicitly uncoupled before editing")
        if cmd.operation in {"fm_deviation", "pm_deviation"}:
            maximum = number(self.transport.query(f"SOUR:{mode}1:DEV? MAX"))
            if parameters["value"] > maximum:
                raise ValidationError("Deviation exceeds current instrument modulation mode limit")
        if cmd.operation in {f"{m}_{op}" for m in ("am", "fm", "pm") for op in ("frequency", "shape")}:
            if self.read(mode.lower() + "_source") != "INT":
                raise ValidationError("Internal modulation controls require LF1 / INT source")
            if self.transport.query("SOUR:LFO:FREQ:MODE?").strip().upper() != b"CW":
                raise ValidationError("Shared LF generator must be in CW mode")
            for ch in (1, 2):
                if number(self.transport.query(f"SOUR:LFO{ch}:STAT?")):
                    raise ValidationError("Shared LF generator may drive an active auxiliary output")
            for other in ("AM", "FM", "PM"):
                for ch in (1, 2):
                    if other == mode and ch == 1:
                        continue
                    if number(self.transport.query(f"SOUR:{other}{ch}:STAT?")):
                        raise ValidationError("Another active modulation path shares the LF generator")
            if cmd.operation.endswith("_frequency"):
                for limit, compare in (("MIN", lambda a, b: a < b), ("MAX", lambda a, b: a > b)):
                    if compare(parameters["value"], number(self.transport.query("SOUR:LFO1:FREQ? " + limit))):
                        raise ValidationError("LF frequency exceeds installed generator range")
        if cmd.operation in {"sweep_start", "sweep_stop"}:
            other = self.read("sweep_stop" if cmd.operation == "sweep_start" else "sweep_start")
            if (cmd.operation == "sweep_start" and parameters["value"] > other) or (
                cmd.operation == "sweep_stop" and parameters["value"] < other
            ):
                raise ValidationError("Sweep start cannot exceed stop")
        if cmd.operation == "sweep_step_linear" and parameters["value"] > self.read("sweep_stop") - self.read(
            "sweep_start"
        ):
            raise ValidationError("Sweep step exceeds span")
        if cmd.operation == "upload_list":
            if self.read("frequency_mode") != "CW":
                raise ValidationError("Leave LIST/SWE mode before editing lists")
            if parameters["name"].lower() in {name.lower() for name in self.list_catalog()}:
                raise ValidationError("Upload requires a new list name; existing lists are preserved")
        if cmd.operation == "fm_deviation":
            carrier = float(self.read("frequency", cmd.channel))
            band = self.capabilities().get("frequency").parameters["value"]
            minimum, maximum = band.minimum or 0, band.maximum or carrier
            if parameters["value"] > min(carrier - minimum, maximum - carrier):
                raise ValidationError("FM deviation exceeds carrier/bandwidth margins")

    def encode(
        self, operation: str, spec: CommandSpec, context: dict[str, Any], parameters: dict[str, Any]
    ) -> tuple[bytes | str, ...]:
        if operation in {"am_source", "fm_source", "pm_source"}:
            value = {"INT": "LF1", "EXT": "EXT1"}.get(parameters["value"], parameters["value"])
            return (spec.writes[0].split(" ")[0] + " " + value,)
        if operation == "sweep_step_log":
            return (f"SOUR:SWE:FREQ:STEP:LOG {parameters['value']:.12g}PCT",)
        if operation == "upload_list":
            return (
                f"SOUR:LIST:SEL '{parameters['name']}'",
                "SOUR:LIST:FREQ " + ",".join((f"{v:.12g}" for v in parameters["frequencies"])),
                "SOUR:LIST:POW " + ",".join((f"{v:.12g}" for v in parameters["powers"])),
            )
        return super().encode(operation, spec, context, parameters)

    def list_catalog(self) -> tuple[str, ...]:
        raw = self.transport.query("SOUR:LIST:CAT?").decode("ascii").strip()
        if raw in {"", "-", '""'}:
            return ()
        names = next(csv.reader([raw]))
        return tuple(name.strip().strip("'\"").rsplit("/", 1)[-1].removesuffix(".lsw") for name in names)

    def read_list(self, include_dwells: bool = False) -> dict[str, Any]:
        if self.transport.query("FORM?").strip().upper() not in {b"ASC", b"ASCII"}:
            raise Unsupported("List backup requires existing ASCII transfer format; format was not changed")
        selected = self.transport.query("SOUR:LIST:SEL?").decode("ascii").strip().strip('"')
        result: dict[str, Any] = {"selected": selected, "raw": {}}
        fields = [("frequencies", "FREQ"), ("powers", "POW")]
        if include_dwells:
            fields.append(("dwells", "DWEL:LIST"))
        for name, command_name in fields:
            count = int(number(self.transport.query(f"SOUR:LIST:{command_name}:POIN?")))
            if not 0 <= count <= 10000:
                raise ValidationError("Unexpected or excessive list point count")
            raw = self.transport.query(f"SOUR:LIST:{command_name}?").decode("ascii").strip() if count else ""
            values = [number(value) for value in raw.split(",")] if raw else []
            if len(values) != count:
                raise ValidationError("List point count does not match complete response")
            result[name], result["raw"][name] = values, raw
        if len(result["frequencies"]) != len(result["powers"]):
            raise ValidationError("Frequency/power list lengths differ")
        return result

    def backup_list_file(self, selected: str) -> bytes:
        """Read the complete existing file, including dwell data not exposed on older firmware."""
        actual = self.transport.query("SOUR:LIST:SEL?").decode("ascii").strip().strip('"')
        if selected != actual:
            raise ValidationError("Selected list changed before file backup")
        data = block_payload(self.transport.query("MMEM:DATA? " + quote_name(selected)))
        if not data:
            raise ValidationError("Empty list file backup")
        return data

    def select_existing_list(self, filename: str) -> None:
        name = filename.rsplit("/", 1)[-1].removesuffix(".lsw")
        if name not in self.list_catalog():
            raise ValidationError("Original list no longer exists; selection would create data")
        self.transport.write("SOUR:LIST:SEL " + quote_name(filename))

    def delete_created_list(self, name: str, created: set[str]) -> None:
        if name not in created or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,23}", name):
            raise ValidationError("Only lists created by this test may be deleted")
        if self.read("frequency_mode") != "CW":
            raise ValidationError("Leave LIST mode before removing temporary data")
        selected = self.transport.query("SOUR:LIST:SEL?").decode("ascii").strip().strip('"')
        if selected.rsplit("/", 1)[-1].removesuffix(".lsw") == name:
            raise ValidationError("Select the original list before deleting test data")
        self.transport.write("SOUR:LIST:DEL " + quote_name(name))

    def apply(self, command: DeviceCommand) -> ApplyResult:
        result = super().apply(command)
        if command.operation == "upload_list" and result.outcome == "success":
            try:
                actual = self.read_list()
                if (
                    actual["frequencies"] != command.parameters["frequencies"]
                    or actual["powers"] != command.parameters["powers"]
                ):
                    return replace(
                        result, outcome="rejected", readback=actual, error="Full list readback differs"
                    )
                return replace(result, readback=actual)
            except DeviceError as exc:
                return replace(result, outcome="partial", error=str(exc))
        return result
