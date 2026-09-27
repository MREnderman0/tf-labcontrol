"""Additional backup/recovery commands; not registered as end-user controls."""

from dataclasses import replace
from functools import partial

from lab_control.instruments.base import command, enum, parameter, read_command

from ..adapter import FRAME_SETTINGS, FphAdapter, agrees, tolerance
from ..profile import fph
from ..protocol import boolean, choice, scalar

OTHER_MARKERS = tuple(f"{kind}{n}_enabled" for n in range(2, 7) for kind in ("marker", "delta"))
EXTRA = (
    "options",
    "continuous",
    "sweep_count",
    "marker_enabled",
    "marker_frequency_mode",
    "marker_search_limits",
    "marker_search_left",
    "marker_search_right",
    "marker_noise",
    "marker_ndb",
    "marker_demodulation",
    "display_range",
    "display_spacing",
    "attenuation_mode",
    "trigger_slope",
    "trace_enabled",
    "trace2_enabled",
    "trace2_mode",
    "detector2",
    "detector2_auto",
    "trace_math",
    *OTHER_MARKERS,
    "marker_frequency",
)
SETTINGS = (*FRAME_SETTINGS, *EXTRA)
AUTOMATIC = {op: op + "_auto" for op in ("rbw", "vbw", "attenuation", "sweep_time", "detector")}


def verification_profile():
    profile = fph()
    commands = dict(profile.commands)
    commands["restore_marker_frequency"] = replace(
        command("restore_marker_frequency", "CALC:MARK1:X", parameter("Hz", 0, 4e9), scoped=False),
        writes=("CALC:MARK1:X {value} Hz",),
        decode=scalar,
        compare=False,
    )
    for name, path, param, decoder in (
        ("transfer_format", "FORM", enum("ASC", "REAL"), partial(choice, choices=("ASC", "REAL"))),
        ("byte_order", "FORM:BORD", enum("NORM", "SWAP"), partial(choice, choices=("NORM", "SWAP"))),
        ("display_range", "DISP:TRAC1:Y", parameter("dB", 1, 150), scalar),
        ("display_spacing", "DISP:TRAC1:Y:SPAC", enum("LOG", "LIN"), partial(choice, choices=("LOG", "LIN"))),
    ):
        commands[name] = replace(command(name, path, param, scoped=False), decode=decoder, compare=False)
    for name, query, decoder in (
        ("marker_search_limits", "CALC:MARK1:X:SLIM:STAT?", boolean),
        ("marker_search_left", "CALC:MARK1:X:SLIM:LEFT?", scalar),
        ("marker_search_right", "CALC:MARK1:X:SLIM:RIGH?", scalar),
        ("marker_noise", "CALC:MARK1:FUNC:NOIS:STAT?", boolean),
        ("marker_ndb", "CALC:MARK1:FUNC:NDBD:STAT?", boolean),
        ("marker_demodulation", "CALC:MARK1:FUNC:DEM:STAT?", boolean),
        ("attenuation_mode", "INP:ATT:MODE?", partial(choice, choices=("LDIS", "LNO"))),
        ("trigger_slope", "TRIG:SLOP?", partial(choice, choices=("POS", "NEG"))),
        ("trace_enabled", "DISP:TRAC1:STAT?", boolean),
        ("trace2_enabled", "DISP:TRAC2:STAT?", boolean),
        (
            "trace2_mode",
            "DISP:TRAC2:MODE?",
            partial(choice, choices=("WRIT", "AVER", "MAXH", "MINH", "VIEW", "FRE", "INF")),
        ),
        (
            "detector2",
            "SENS:DET2:FUNC?",
            partial(choice, choices=("POS", "NEG", "SAMP", "RMS", "AVER", "APE")),
        ),
        ("detector2_auto", "SENS:DET2:FUNC:AUTO?", boolean),
        ("trace_math", "CALC1:MATH:STAT?", boolean),
    ):
        commands[name] = replace(read_command(name, query, scoped=False), decode=decoder)
    for n in range(2, 7):
        for kind, mnemonic in (("marker", "MARK"), ("delta", "DELT")):
            name = f"{kind}{n}_enabled"
            commands[name] = replace(
                read_command(name, f"CALC:{mnemonic}{n}:STAT?", scoped=False), decode=boolean
            )
    return replace(profile, commands=commands)


class VerificationAdapter(FphAdapter):
    """Permit restoring a backed-up off-screen marker, never a public control."""

    def preflight(self, command, parameters):
        super().preflight(command, parameters)
        if command.operation == "restore_marker_frequency":
            from lab_control.domain.instruments import ValidationError

            if (
                parameters["value"] > self.maximum_frequency
                or not self.read("marker_enabled")
                or self.read("marker_frequency_mode") != "FREQ"
                or self.read("span") == 0
                or self.read("frequency_offset") != 0
                or any(self.read(op) for op in OTHER_MARKERS)
            ):
                raise ValidationError("Backed-up marker cannot be restored in the current state")


def differences(actual: dict, expected: dict) -> dict:
    result = {}
    for op, value in expected.items():
        tol = tolerance(op, value)
        if op == "marker_frequency" and value is not None:
            tol = expected["span"] / 710 / 2 + 0.5
        equal = (
            (abs(actual[op] - value) <= tol)
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and isinstance(actual.get(op), (int, float))
            )
            else agrees(op, value, actual.get(op))
        )
        if not equal:
            result[op] = {"expected": value, "actual": actual.get(op), "tolerance": tol}
    return result


def validate_target(target: dict, adapter: FphAdapter) -> None:
    from lab_control.domain.instruments import DeviceCommand, DeviceError

    if set(target) != set(SETTINGS):
        raise DeviceError("Incomplete FPH restoration snapshot", "backup_unreliable")
    if any(
        target[k]
        for k in (
            *OTHER_MARKERS,
            "marker_search_limits",
            "marker_noise",
            "marker_ndb",
            "marker_demodulation",
            "trace_math",
            "preamplifier",
        )
    ):
        raise DeviceError(
            "Active auxiliary marker/correction/preamp has no qualified restoration path", "backup_unreliable"
        )
    if (
        target["measurement_mode"] != "SAN"
        or target["frequency_offset"]
        or target["reference_offset"]
        or target["amplitude_unit"] != "DBM"
        or target["marker_frequency_mode"] != "FREQ"
        or target["trigger_source"] != "IMM"
        or target["span"] == 0
        or target["display_spacing"] != "LOG"
        or target["trace_mode"] != "WRIT"
    ):
        raise DeviceError(
            "Snapshot outside qualified ordinary-spectrum recovery envelope", "backup_unreliable"
        )
    # Validate the destination schema, not the current transient (e.g. zero-span)
    # UI schema. This is pure validation; it cannot read or write the instrument.
    destination = VerificationAdapter(adapter.transport, adapter.profile, adapter.config)
    destination.detected_options = adapter.detected_options
    destination.dynamic_parameters = dict(adapter.dynamic_parameters)
    destination.capability_details = dict(target)
    for op, value in target.items():
        if adapter.profile.commands[op].capability.writable and value is not None:
            destination.validate_command(DeviceCommand(op, {"value": value}, confirmed=True))


def restore(test, target: dict) -> None:
    """Stop on the first failed restore command/readback; never catch and continue."""
    test.apply("continuous", False)
    test.apply("amplitude_unit", target["amplitude_unit"])
    test.apply("span", 0)
    test.apply("center_frequency", target["center_frequency"])
    test.apply("span", target["span"])
    for op in AUTOMATIC:
        test.apply(op + "_auto", False)
    # Attenuator changes may clamp reference level: restore attenuation first.
    for op in (
        "attenuation",
        "reference_level",
        "rbw",
        "vbw",
        "sweep_time",
        "trace_mode",
        "detector",
        "sweep_count",
    ):
        test.apply(op, target[op])
    for op, auto in AUTOMATIC.items():
        test.apply(auto, target[auto])
    for op in ("display_spacing", "display_range", "trigger_source", "byte_order", "transfer_format"):
        test.apply(op, target[op])
    if target["marker_enabled"]:
        test.apply("marker_enabled", True)
        test.apply("restore_marker_frequency", target["marker_frequency"])
    test.apply("marker_enabled", target["marker_enabled"])
    test.apply("continuous", target["continuous"])
