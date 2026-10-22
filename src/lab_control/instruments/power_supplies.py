"""Deprecated import compatibility; implementations live in model packages."""

from lab_control.instruments.devices.itech_it6322.adapter import ITECH_SOURCE, it6322
from lab_control.instruments.devices.keithley_2231a.adapter import BOOL, KEITHLEY_SOURCE, keithley
from lab_control.instruments.devices.siglent_spd4306x.adapter import SPD_SOURCE, spd
from lab_control.instruments.devices.siglent_spd4306x.adapter import Spd4306xAdapter as PowerAdapter

PROFILES = (keithley(), spd(), it6322())

__all__ = [
    "keithley",
    "KEITHLEY_SOURCE",
    "BOOL",
    "spd",
    "SPD_SOURCE",
    "PowerAdapter",
    "it6322",
    "ITECH_SOURCE",
    "PROFILES",
]
