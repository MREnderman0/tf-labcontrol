"""Acceptance-only backup, frozen-format comparison and dependency-ordered restoration."""

from __future__ import annotations

from typing import Any

import numpy as np

from lab_control.domain.instruments import DeviceCommand, DeviceError

from ..adapter import FRAME_SETTINGS, WAVELENGTHS, Ceyear6362DAdapter, agrees
from ..protocol import RESOLUTIONS, values, wavelength_axis

SETTINGS = (*FRAME_SETTINGS, "active_trace")
MATRIX = (
    ("points_auto", False),
    ("rbw", 0.1),
    ("rbw", 0.2),
    ("sweep_points", 1001),
    ("sweep_points", 2001),
    ("sensitivity", "NORMAL"),
    ("sensitivity", "MID"),
)
# Fixed before any write; test points are exact settings, not a physical accuracy claim.
TOLERANCES = {op: 1e-6 for op in WAVELENGTHS}


def snapshot(adapter: Ceyear6362DAdapter) -> dict[str, Any]:
    return {op: adapter.read(op) for op in SETTINGS}


def differences(actual: dict, expected: dict) -> dict:
    return {
        op: {"expected": value, "actual": actual.get(op), "absolute_tolerance": TOLERANCES.get(op, 0)}
        for op, value in expected.items()
        if not agrees(op, value, actual.get(op))
    }


def validate_backup(state: dict, original_scan: str, trigger: str, *, leave_stopped: bool = False) -> None:
    if set(state) != set(SETTINGS):
        raise DeviceError("Incomplete 6362D snapshot", "backup_unreliable")
    if not leave_stopped and (original_scan not in {"stopped", "repeat"} or trigger not in {"absent", "off"}):
        raise DeviceError("Panel-confirmed scan and H04 trigger state required", "backup_unreliable")
    if (
        state["axis_mode"] != "WAVELENGTH"
        or state["amplitude_unit"] != "dBm"
        or state["trace_mode"] not in ({"WRITE", "FIX"} if leave_stopped else {"WRITE"})
        or state["sweep_mode"] == "AUTO"
        or state["marker_scan_range"]
        or state["wavelength_span"] <= 0
        or state["rbw"] not in RESOLUTIONS
        or state["active_trace"] not in {"TR" + letter for letter in "ABCDEFG"}
        or (original_scan == "repeat" and state["sweep_mode"] != "REPEAT")
    ):
        raise DeviceError("Backup outside qualified restoration envelope", "backup_unreliable")


def stop_scan(adapter: Ceyear6362DAdapter) -> dict:
    """ABORT before ordinary queries: a pending single scan blocks those queries."""
    adapter.transport.write(":ABORT")
    adapter.wait_sweep()
    mode = adapter.read("sweep_mode")
    if mode == "AUTO":
        raise DeviceError("ABORT cannot confirm AUTO stopped", "backup_unreliable")
    return {"abort_sent": True, "condition": 1, "mode": mode, "restarted": False}


def trace_attributes(adapter: Ceyear6362DAdapter) -> dict[str, int]:
    from ..protocol import integer

    return {
        "TR" + letter: integer(adapter.transport.query(f":TRACE:ATTRIBUTE:TR{letter}?"))
        for letter in "ABCDEFG"
    }


def set_trace_attribute(adapter: Ceyear6362DAdapter, trace: str, attribute: int) -> None:
    from ..protocol import integer

    if trace not in {"TR" + letter for letter in "ABCDEFG"} or attribute not in {0, 1}:
        raise DeviceError("Only backed-up WRITE/FIX trace attributes may be changed", "backup_unreliable")
    adapter.transport.write(f":TRACE:ATTRIBUTE:{trace} " + ("WRITE" if attribute == 0 else "FIX"))
    if integer(adapter.transport.query(f":TRACE:ATTRIBUTE:{trace}?")) != attribute:
        raise DeviceError("Trace attribute readback mismatch", "readback_mismatch")


def select_trace(adapter: Ceyear6362DAdapter, trace: str) -> None:
    if trace not in {"TR" + letter for letter in "ABCDEFG"}:
        raise DeviceError("Invalid backed-up trace", "backup_unreliable")
    adapter.transport.write(f":TRACE:ACTIVE {trace}")
    if adapter.read("active_trace") != trace:
        raise DeviceError("Active trace readback mismatch", "readback_mismatch")


def prepare_control_traces(adapter: Ceyear6362DAdapter, attributes: dict[str, int]) -> None:
    if set(attributes) != {"TR" + letter for letter in "ABCDEFG"} or set(attributes.values()) - {0, 1}:
        raise DeviceError("Trace backup must contain seven WRITE/FIX attributes", "backup_unreliable")
    for trace, attribute in attributes.items():
        if trace != "TRA" and attribute == 0:
            set_trace_attribute(adapter, trace, 1)
    if attributes["TRA"] != 0:
        set_trace_attribute(adapter, "TRA", 0)
    select_trace(adapter, "TRA")


def checked_apply(adapter: Ceyear6362DAdapter, operation: str, value: Any = None):
    result = adapter.apply(
        DeviceCommand(operation, {} if value is None else {"value": value}, confirmed=True)
    )
    if result.outcome != "success":
        raise DeviceError(f"{operation}: {result.outcome}: {result.error}", "readback_mismatch")
    return result


def set_endpoints(apply, read, start: float, stop: float) -> None:
    """Expand the appropriate endpoint first, without crossing the current interval."""
    current_start = read("start_wavelength")
    if stop <= current_start:
        apply("start_wavelength", start)
        apply("stop_wavelength", stop)
    else:
        apply("stop_wavelength", stop)
        apply("start_wavelength", start)


def restore(
    adapter: Ceyear6362DAdapter,
    state: dict,
    original_scan: str,
    trigger: str,
    *,
    leave_stopped: bool = False,
    attributes: dict[str, int] | None = None,
) -> dict:
    """Each recovery command runs at most once; first failure stops recovery."""
    validate_backup(state, original_scan, trigger, leave_stopped=leave_stopped)

    def apply(op, value=None):
        return checked_apply(adapter, op, value)

    stopping = stop_scan(adapter)
    apply("points_auto", False)
    set_endpoints(apply, adapter.read, state["start_wavelength"], state["stop_wavelength"])
    apply("rbw", state["rbw"])
    apply("sensitivity", state["sensitivity"])
    apply("sweep_points", state["sweep_points"])
    apply("points_auto", state["points_auto"])
    adapter.set_transfer_format(state["transfer_format"])
    if attributes is not None:
        current = trace_attributes(adapter)
        for trace, attribute in attributes.items():
            if current[trace] != attribute:
                set_trace_attribute(adapter, trace, attribute)
        select_trace(adapter, state["active_trace"])
        if trace_attributes(adapter) != attributes:
            raise DeviceError("Trace restoration differs", "restoration_failed")
    apply("sweep_mode", state["sweep_mode"])
    actual = snapshot(adapter)
    delta = differences(actual, state)
    if delta:
        raise DeviceError(f"6362D restoration differs: {delta}", "restoration_failed")
    if original_scan == "repeat" and not leave_stopped:
        apply("repeat")
    return {
        "snapshot": actual,
        "differences": delta,
        "scan_command": "leave_stopped" if leave_stopped else original_scan,
        "stop_evidence": stopping,
        "scan_state_confirmation": "ABORT completed; no restart" if leave_stopped else "panel_confirmation_required",
        "historical_curves_restored": False,
    }


def qualify_binary(
    ascii_xy: tuple[bytes, bytes],
    binary_xy: tuple[bytes, bytes],
    count: int,
    *,
    x_atol_nm: float,
    y_atol_db: float,
) -> dict:
    """Offline acceptance search only. Runtime decoding never tries alternate orders."""
    if not 0 < x_atol_nm <= 0.001 or not 0 < y_atol_db <= 0.001:
        raise DeviceError(
            "Comparison precision must be fixed from ASCII reply precision", "invalid_parameter"
        )
    expected = (wavelength_axis(ascii_xy[0], count, "ASCII", None), values(ascii_xy[1], count, "ASCII", None))
    matched = []
    evidence: dict[str, Any] = {}
    for order in ("<", ">"):
        try:
            actual = (
                wavelength_axis(binary_xy[0], count, "REAL,64", order),
                values(binary_xy[1], count, "REAL,64", order),
            )
            errors = [float(np.max(np.abs(x - y))) for x, y in zip(expected, actual, strict=True)]
            evidence[order] = errors
            if errors[0] <= x_atol_nm and errors[1] <= y_atol_db:
                matched.append(order)
        except DeviceError as exc:
            evidence[order] = str(exc)
    if len(matched) != 1:
        raise DeviceError(f"No unique REAL64 qualification: {evidence}", "qualification_failed")
    return {
        "byte_order": matched[0],
        "points": count,
        "errors_nm_db": evidence,
        "x_atol_nm": x_atol_nm,
        "y_atol_db": y_atol_db,
    }
