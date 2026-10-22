"""Category applicability and slot rules shared by all application frontends."""

from typing import Any

from lab_control.domain.workspace import WORKSPACE_MODES
from lab_control.instruments.registry import DEVICE_DEFINITIONS, PROFILES, SDK_ADAPTERS


def instrument_category(record: dict[str, Any]) -> str:
    adapter = record.get("adapterId", "")
    if adapter.startswith("sim.") and record.get("config", {}).get("transport") == "mock":
        return adapter.removeprefix("sim.")
    return PROFILES[adapter].category if adapter in PROFILES else SDK_ADAPTERS.get(adapter, "unknown")


def mode_applicable(record: dict[str, Any], mode: str) -> bool:
    category = instrument_category(record)
    # Legacy mode values remain valid metadata, never a device-type gate.
    return mode in WORKSPACE_MODES and category != "unknown"


def surface_applicable(record: dict[str, Any], surface: str) -> bool:
    """Return whether a connected model belongs on a workspace surface.

    Surface policy is intentionally scoped to power supplies. Other device
    categories keep their existing module routing, while un-packaged or
    simulated power supplies retain the historical monitoring-only behavior.
    """
    if surface not in {"management", "monitoring"}:
        raise ValueError(f"Unknown workspace surface: {surface}")
    if instrument_category(record) != "power_supply":
        return True
    definition = DEVICE_DEFINITIONS.get(record.get("adapterId", ""))
    if definition is None:
        return surface == "monitoring"
    return definition.management_surface if surface == "management" else definition.monitoring_surface
