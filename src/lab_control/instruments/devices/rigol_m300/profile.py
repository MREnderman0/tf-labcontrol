"""RIGOL M300 Programming Guide SCPI contract."""

from dataclasses import replace
from functools import partial

from lab_control.domain.instruments import Parameter
from lab_control.instruments.base import Profile, command, enum, number, parameter, read_command

from .protocol import boolean, choice, configuration, csv_numbers, integer, module_identity, scan_list, token

SOURCE = "https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/data-aquisition/M300_ProgrammingGuide_EN.pdf"


def m300() -> Profile:
    commands = {
        "trigger_source": command(
            "trigger_source",
            "TRIG:SOUR",
            enum("IMM", "TIM", "BUS", "EXT", "ALAR1", "ALAR2", "ALAR3", "ALAR4", "ABS"),
            scoped=False,
        ),
        "trigger_interval": command(
            "trigger_interval", "TRIG:TIM", parameter("s", 0, 359999.999), scoped=False
        ),
        "monitor_state": command(
            "monitor_state", "ROUT:MON:STAT", Parameter("boolean"), scoped=False, confirm=True
        ),
        "dmm_state": command("dmm_state", "INST:DMM", Parameter("boolean"), scoped=False, confirm=True),
        "scan_list": read_command("scan_list", "ROUT:SCAN?", scoped=False),
        "scan_size": read_command("scan_size", "ROUT:SCAN:SIZE?", scoped=False),
        "dmm_installed": read_command("dmm_installed", "INST:DMM:INST?", scoped=False),
        "configuration": read_command("configuration", "CONF?", scoped=False),
        "errors": read_command("errors", "SYST:ERR?", scoped=False),
        "system_version": read_command("system_version", "SYST:VERS?", scoped=False),
        "measurements": read_command("measurements", "READ?", scoped=False),
    }
    commands["trigger_source"] = replace(commands["trigger_source"], decode=partial(choice, choices=("IMM", "TIM", "BUS", "EXT", "ALAR1", "ALAR2", "ALAR3", "ALAR4", "ABS")))
    commands["trigger_interval"] = replace(commands["trigger_interval"], decode=number)
    commands["monitor_state"] = replace(commands["monitor_state"], decode=boolean)
    commands["dmm_state"] = replace(commands["dmm_state"], decode=boolean)
    commands["scan_list"] = replace(commands["scan_list"], decode=scan_list)
    commands["scan_size"] = replace(commands["scan_size"], decode=integer)
    commands["dmm_installed"] = replace(commands["dmm_installed"], decode=boolean)
    commands["configuration"] = replace(commands["configuration"], decode=configuration)
    commands["errors"] = replace(commands["errors"], decode=token)
    commands["system_version"] = replace(commands["system_version"], decode=token)
    commands["measurements"] = replace(commands["measurements"], decode=csv_numbers)
    for slot in (100, 200, 300, 400, 500):
        name = f"slot_{slot}"
        commands[name] = replace(
            read_command(name, f"SYST:CTYP? {slot}", scoped=False), decode=module_identity
        )
    return Profile(
        "rigol.m300",
        ("RIGOL", "RIGOL TECHNOLOGIES"),
        ("M300",),
        "multimeter",
        (1,),
        SOURCE,
        "M300 Programming Guide v2.0; chapters 2-21, 2-61, 2-101..105, 2-191..194, 4",
        commands,
        (
            "scan_list",
            "scan_size",
            "dmm_installed",
            "dmm_state",
            "trigger_source",
            "trigger_interval",
            "monitor_state",
            "slot_100",
            "slot_200",
            "slot_300",
            "slot_400",
            "slot_500",
            "errors",
        ),
        acquisition="scalar",
    )
