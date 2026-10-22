"""Deprecated SDK imports; implementation lives in its independent model package."""

from lab_control.instruments.devices.pico_6404e.adapter import (
    F64,
    I16,
    I32,
    PTR,
    RANGES,
    RAW,
    SOURCE,
    U32,
    U64,
    PicoAdapter,
    StreamingInfo,
    TriggerInfo,
    enumerate_pico,
)

__all__ = [
    "SOURCE",
    "I16",
    "I32",
    "U32",
    "U64",
    "F64",
    "PTR",
    "RAW",
    "RANGES",
    "StreamingInfo",
    "TriggerInfo",
    "PicoAdapter",
    "enumerate_pico",
]
