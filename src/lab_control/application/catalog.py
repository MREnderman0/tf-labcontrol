"""Category applicability and slot rules shared by all application frontends."""

from typing import Any

from lab_control.instruments.registry import PROFILES, SDK_ADAPTERS


def instrument_category(record: dict[str, Any]) -> str:
    adapter = record.get("adapterId", "")
    if adapter.startswith("sim.") and record.get("config", {}).get("transport") == "mock":
        return adapter.removeprefix("sim.")
    return PROFILES[adapter].category if adapter in PROFILES else SDK_ADAPTERS.get(adapter, "unknown")


def mode_applicable(record: dict[str, Any], mode: str) -> bool:
    category = instrument_category(record)
    if mode not in {"electronic", "optical"} or category == "unknown":
        return False
    if category in {"optical_power_meter", "optical_spectrometer"}:
        return mode == "optical"
    if category == "spectrum_analyzer":
        return mode == "electronic"
    return True
