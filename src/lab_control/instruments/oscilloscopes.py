"""Deprecated profile imports; each implementation lives in its model package."""

from lab_control.instruments.devices.lecroy_waverunner9254.adapter import LECROY_SOURCE, lecroy, lecroy_value
from lab_control.instruments.devices.rigol_ds1202ze.adapter import DS_SOURCE, rigol
from lab_control.instruments.devices.rs_rtm3004.adapter import BOOL, RTM_SOURCE, rtm3004
from lab_control.instruments.devices.rs_rto64.adapter import RTO_SOURCE, rto64


def rs_scope(rto: bool = False):
    return rto64() if rto else rtm3004()


PROFILES = (rs_scope(), rs_scope(True), rigol(), lecroy("WaveRunner9254"))

__all__ = [
    "rtm3004",
    "RTM_SOURCE",
    "BOOL",
    "rto64",
    "RTO_SOURCE",
    "rigol",
    "DS_SOURCE",
    "lecroy",
    "lecroy_value",
    "LECROY_SOURCE",
    "rs_scope",
    "PROFILES",
]
