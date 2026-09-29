"""Documented 6362D capabilities; UI values in nm, protocol query values in m."""

from dataclasses import replace
from functools import partial
from typing import Any, Callable

from lab_control.domain.instruments import Parameter
from lab_control.instruments.base import Profile, command, enum, parameter, read_command

from .protocol import (
    POWER_UNITS,
    RESOLUTIONS,
    SENSITIVITIES,
    SWEEP_MODES,
    boolean,
    data_format,
    integer,
    mapped,
    nanometers,
    scalar,
    token,
)

SOURCE = "https://www.ceyear.com/optical_spectrum_test-163?act=97&type=file"
REVISION = "6362 series Programming Manual B.11, 2025-08; software >=2.1.0; pp.5,17,88-101,105-114"


def profile() -> Profile:
    commands = {}
    decode: Callable[[bytes], Any]
    for name, path, limit in (
        ("start_wavelength", ":SENSE:WAVELENGTH:START", parameter("nm", 600, 1700)),
        ("stop_wavelength", ":SENSE:WAVELENGTH:STOP", parameter("nm", 600, 1700)),
        ("center_wavelength", ":SENSE:WAVELENGTH:CENTER", parameter("nm", 600, 1700)),
        ("wavelength_span", ":SENSE:WAVELENGTH:SPAN", parameter("nm", 0.001, 1100)),
        (
            "rbw",
            ":SENSE:BANDWIDTH:RESOLUTION",
            replace(parameter("nm", 0.02, 2), choices=tuple(str(x) for x in RESOLUTIONS)),
        ),
    ):
        spec = command(name, path, limit, scoped=False)
        commands[name] = replace(spec, writes=(path + " {value}NM",), decode=nanometers, compare=False)
    for name, path, limit, decode in (
        (
            "sensitivity",
            ":SENSE:SENSE",
            enum(*SENSITIVITIES.values()),
            partial(mapped, choices=SENSITIVITIES),
        ),
        ("sweep_points", ":SENSE:SWEEP:POINTS", Parameter("integer", "point", 101, 50001), integer),
        ("points_auto", ":SENSE:SWEEP:POINTS:AUTO", Parameter("boolean"), boolean),
        (
            "sweep_mode",
            ":INITIATE:SMODE",
            enum("SINGLE", "REPEAT", "SEGMENT"),
            partial(mapped, choices=SWEEP_MODES),
        ),
    ):
        commands[name] = replace(command(name, path, limit, scoped=False), decode=decode, compare=False)
    for name, path, decode in (
        ("transfer_format", ":FORMAT:DATA?", data_format),
        ("axis_mode", ":UNIT:X?", partial(mapped, choices={0: "WAVELENGTH", 1: "FREQUENCY"})),
        ("amplitude_unit", ":DISPLAY:TRACE:Y1:UNIT?", partial(mapped, choices=POWER_UNITS)),
        (
            "trace_mode",
            ":TRACE:ATTRIBUTE:TRA?",
            partial(mapped, choices={0: "WRITE", 1: "FIX", 2: "MAX", 3: "MIN", 4: "RAVG", 5: "CALC"}),
        ),
        ("trace_points", ":TRACE:DATA:SNUMBER? TRA", integer),
        ("active_trace", ":TRACE:ACTIVE?", token),
        ("operation_condition", ":STATUS:OPERATION:CONDITION?", partial(mapped, choices={0: 0, 1: 1})),
        (
            "wavelength_medium",
            ":SENSE:CORRECTION:RVELOCITY:MEDIUM?",
            partial(mapped, choices={0: "AIR", 1: "VACUUM"}),
        ),
        ("wavelength_offset", ":SENSE:CORRECTION:WAVELENGTH:SHIFT?", nanometers),
        ("reference_offset", ":SENSE:CORRECTION:LEVEL:SHIFT?", scalar),
        ("marker_scan_range", ":SENSE:WAVELENGTH:SRANGE?", boolean),
    ):
        commands[name] = replace(read_command(name, path, scoped=False), decode=decode)
    commands["single"] = replace(
        command("single", ":INITIATE", read=False, scoped=False, confirm=True),
        writes=(":INITIATE:SMODE SINGLE", ":INITIATE"),
    )
    commands["repeat"] = replace(
        command("repeat", ":INITIATE", read=False, scoped=False, confirm=True),
        writes=(":INITIATE:SMODE REPEAT", ":INITIATE"),
    )
    commands["stop"] = command("stop", ":ABORT", read=False, scoped=False, confirm=True)
    return Profile(
        "ceyear.6362d",
        ("Ceyear",),
        ("6362D", "CY6362D"),
        "optical_spectrometer",
        (1,),
        SOURCE,
        REVISION,
        commands,
        ("start_wavelength", "stop_wavelength", "rbw", "sensitivity", "sweep_mode"),
        error_query="",
        acquisition="ceyear_6362d",
    )
