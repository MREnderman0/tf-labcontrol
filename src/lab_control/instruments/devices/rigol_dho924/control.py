"""DHO924 control dependencies and verified, timestamp-preserving overlays."""

from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING

from lab_control.domain.instruments import ApplyResult, ControlUpdate, DeviceCommand, ParameterReading
from lab_control.domain.oscilloscope import CONTROL_NAMES

if TYPE_CHECKING:
    from .adapter import DHO924Adapter


def affected(adapter: "DHO924Adapter", command: DeviceCommand) -> set[tuple[int, str]] | None:
    op, ch = command.operation, command.channel
    keys = {(ch, op)}
    if op in {"scale", "offset", "coupling", "probe_attenuation"}:
        if op != "offset":
            keys.update((ch, name) for name in ("scale", "offset", "probe_attenuation", "coupling"))
        if adapter._settings.get("1.trigger_source") == f"CHAN{ch}":
            keys.add((1, "trigger_level"))
    elif op in {"timebase", "time_position", "record_length", "channel_enabled"}:
        keys.update((1, name) for name in ("timebase", "time_position", "record_length", "sample_rate"))
        if op == "timebase":
            # Auto ROLL may change the actual MAIN/ROLL mode with the scale.
            keys.update((1, name) for name in ("roll_enabled", "timebase_mode"))
        if op == "channel_enabled":
            keys.update((ch, name) for name in ("scale", "offset", "coupling", "probe_attenuation"))
            keys.update((1, name) for name in ("trigger_source", "trigger_level", "trigger_slope"))
    elif op == "trigger_source":
        keys.update((1, name) for name in ("trigger_level", "trigger_slope", "trigger_type"))
    elif op in {"trigger_mode", "run", "stop", "single"}:
        keys.discard((ch, op))
        keys.update((1, name) for name in ("trigger_mode", "trigger_sweep", "trigger_status"))
    elif op not in {"trigger_level", "trigger_slope"}:
        return None
    return keys


def apply_control(adapter: "DHO924Adapter", command: DeviceCommand) -> ApplyResult:
    keys = affected(adapter, command)
    context, transfer = deepcopy(adapter._context_cache), adapter._transfer.copy()
    try:
        result = adapter._apply_command(command)
        if result.outcome != "success":
            adapter.invalidate_acquisition_context("error")
            return result
        if keys is None:
            adapter._ranges_dirty = True
            adapter.refresh_capabilities()
            keys = {tuple_key(key) for key in adapter._settings}
            context = None
        else:
            for ch, name in sorted(keys - {(command.channel, command.operation)}):
                adapter.read(name, ch)
            adapter._ranges_dirty = False
            adapter.capability_details.update(
                acquisition_state=adapter._settings.get("1.trigger_mode"),
                memory={
                    "points": adapter._settings["1.record_length"],
                    "sample_rate": adapter._settings["1.sample_rate"],
                },
            )
        if adapter.acquisition_setup_needed():
            adapter.prepare_acquisition_state()
            keys = {tuple_key(key) for key in adapter._settings}
            context = None
        if context:
            values, stamps, signatures = deepcopy(context[1]), dict(context[2]), dict(context[3])
            for ch, name in keys:
                key = f"{ch}.{name}"
                if key in values:
                    values[key], stamps[key] = adapter._settings[key], adapter._stamps[key]
            if command.operation in {"timebase", "time_position", "record_length", "channel_enabled"}:
                signatures.clear()
            elif command.operation in {"scale", "offset", "coupling", "probe_attenuation"}:
                signatures.pop(command.channel, None)
            # The first post-control descriptor is still bound to all screen settings
            # by check_screen and checked again after DATA; only the old signature expires.
            adapter._context_cache = (context[0], values, stamps, signatures)
            adapter._transfer = transfer
        readings = tuple(
            ParameterReading(
                name, ch, value, "instrument" if (ch, name) in keys else "control_cache", adapter._stamps[key]
            )
            for key, value in adapter._settings.items()
            for ch, name in (tuple_key(key),)
            if key in adapter._stamps
        )
        required = {
            (ch if adapter.profile.commands[name].capability.channel_scoped else 1, name)
            for ch in adapter.profile.channels
            for name in (*CONTROL_NAMES, "record_length", "coupling", "probe_attenuation")
        }
        complete = required <= {(r.channel, r.operation) for r in readings}
        return replace(result, control_update=ControlUpdate(readings, tuple(sorted(keys)), complete))
    except Exception:
        adapter.invalidate_acquisition_context("error")
        raise


def tuple_key(key: str) -> tuple[int, str]:
    channel, name = key.split(".", 1)
    return int(channel), name
