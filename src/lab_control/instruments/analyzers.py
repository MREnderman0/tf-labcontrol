"""Deprecated profile imports; implementations live in model packages."""

from lab_control.instruments.devices.keysight_3458a.adapter import DMM_SOURCE, multimeter
from lab_control.instruments.devices.rs_znb3004.adapter import BOOL, ZNB_SOURCE, network

PROFILES = (network(), multimeter())

__all__ = ["network", "ZNB_SOURCE", "BOOL", "multimeter", "DMM_SOURCE", "PROFILES"]
