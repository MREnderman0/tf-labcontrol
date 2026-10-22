"""Deprecated import compatibility; implementations live in model packages."""

from lab_control.instruments.devices.siglent_sdg6032x.adapter import BOOL, SDG_SOURCE, sdg6032x
from lab_control.instruments.devices.siglent_sdg6032x.adapter import Sdg6032xAdapter as SignalGeneratorAdapter

PROFILES = (sdg6032x(),)

__all__ = ["SignalGeneratorAdapter", "sdg6032x", "SDG_SOURCE", "BOOL", "PROFILES"]
