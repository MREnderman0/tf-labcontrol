"""Shared selection policy for overview and category monitoring pages."""

from lab_control.instruments.registry import DEVICE_DEFINITIONS


def instrument_scan_channels(controller, device_id: str) -> tuple[int, ...] | None:
    """Use the model's cached scan list without any UI-side instrument queries."""
    snapshot = controller.runtime.sessions.capability_snapshot(device_id)
    details = snapshot.details if snapshot else {}
    if details.get("acquisition_channel_selection") == "scan_list":
        return tuple(details.get("acquisition_channels", ()))
    return None


def selection_key(controller, device_id: str) -> str:
    record = controller.records.get(device_id, {})
    slot = record.get("slots", {}).get("general", "")
    return f"slot:general:{slot}" if slot else f"device:{device_id}"


def single_input(controller, device_id: str) -> bool:
    definition = DEVICE_DEFINITIONS.get(controller.records.get(device_id, {}).get("adapterId", ""))
    return bool(definition and definition.acquisition_channel_selection == "single")


def selected_input(controller, device_id: str, available: tuple[int, ...]) -> tuple[int, ...]:
    if not available:
        return ()
    saved = controller.runtime.repository.load_workspace("monitoring_channels/frequency_counter", {})
    wanted = saved.get(selection_key(controller, device_id), ()) if isinstance(saved, dict) else ()
    if isinstance(wanted, (list, tuple)):
        valid = tuple(ch for ch in available if ch in wanted)
        if len(valid) == 1:
            return valid
    snapshot = controller.runtime.sessions.capability_snapshot(device_id)
    current = snapshot.details.get("active_channel") if snapshot else None
    return (current,) if current in available else available[:1]
