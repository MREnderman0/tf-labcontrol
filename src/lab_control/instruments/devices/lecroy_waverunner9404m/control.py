"""Model-owned dependencies for verified scalar and CVAR cache updates."""

from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING

from lab_control.domain.instruments import (
    ApplyResult,
    ControlUpdate,
    DeviceCommand,
    DeviceError,
    ParameterReading,
    ValidationError,
)

from .protocol import numeric, trigger_setup, value_text
from .ranges import MEMORY_QUERIES, PATHS, fixed_rate_timebase, read_range

if TYPE_CHECKING:
    from .adapter import WaveRunner9404MAdapter

VERTICAL = {"scale", "offset", "coupling", "probe_attenuation"}
HORIZONTAL = {"timebase", "time_position", "record_length"}
VERTICAL_FIELDS = {"gain", "offset", "vertical_gain_code", "vertical_vernier", "acquisition_vertical_offset"}
HORIZONTAL_FIELDS = {"timebase_code", "horizontal_interval", "raw_point_count", "first_valid", "last_valid"}


def dependencies(command: DeviceCommand) -> set[tuple[int, str]] | None:
    op, ch = command.operation, command.channel
    if op in VERTICAL:
        names = {"offset"} if op == "offset" else VERTICAL
        return {(ch, name) for name in names | {"trigger_level"}}
    if op in HORIZONTAL:
        return {(1, name) for name in HORIZONTAL}
    if op == "channel_enabled":
        return {(ch, op), *((1, name) for name in HORIZONTAL)}
    if op == "trigger_source":
        return {
            (1, "trigger_setup"),
            (1, "trigger_source"),
            (int(command.parameters["value"][1:]), "trigger_level"),
            (int(command.parameters["value"][1:]), "trigger_slope"),
        }
    if op in {"trigger_level", "trigger_slope", "trigger_mode"}:
        return {(ch, op)}
    if op in {"run", "stop", "single"}:
        return {(1, "trigger_mode")}
    return None


def update_ranges(
    adapter: "WaveRunner9404MAdapter", command: DeviceCommand, keys: set[tuple[int, str]]
) -> None:
    # Offset and trigger level do not alter their own legal endpoints. Scale,
    # coupling/probe and horizontal changes can alter dependent CVAR limits.
    op = command.operation
    if op in HORIZONTAL | {"channel_enabled"}:
        memory = {
            key: value_text(adapter.transport.query(query))
            if key == "mode"
            else numeric(adapter.transport.query(query))
            for key, query in MEMORY_QUERIES.items()
        }
        adapter.capability_details["memory"] = memory
        if (
            float(memory["points"]) < 1
            or float(memory["points"]) != int(float(memory["points"]))
            or float(memory["sample_rate"]) <= 0
        ):
            raise DeviceError("Invalid current acquisition memory/rate", "parse_error")
    if op == "offset":
        ch = command.channel
        adapter._ranges[ch, "trigger_level"] = read_range(
            adapter.transport,
            "trigger_level",
            ch,
            adapter.profile.commands["trigger_level"].capability.parameters["value"],
        )
    if op in (VERTICAL - {"offset"}) | HORIZONTAL | {"channel_enabled", "trigger_source"}:
        for ch, name in keys:
            if name in PATHS:
                adapter._ranges[ch, name] = read_range(
                    adapter.transport, name, ch, adapter.profile.commands[name].capability.parameters["value"]
                )
        memory = adapter.capability_details.get("memory", {})
        if op in HORIZONTAL | {"channel_enabled"} and memory.get("mode") == "FixedSampleRate":
            adapter._ranges[1, "timebase"] = fixed_rate_timebase(
                adapter._ranges[1, "timebase"], memory["sample_rate"]
            )
    adapter._ranges_dirty = False


def apply_control(adapter: "WaveRunner9404MAdapter", command: DeviceCommand) -> ApplyResult:
    from .adapter import CHANNEL_SETTINGS

    adapter.validate_command(command)
    required = {
        f"1.{name}"
        for name in ("timebase", "time_position", "trigger_mode", "record_length", "trigger_setup")
    } | {f"{ch}.{name}" for ch in adapter.profile.channels for name in CHANNEL_SETTINGS}
    complete = required <= getattr(adapter, "_settings", {}).keys() and bool(getattr(adapter, "_ranges", {}))
    keys = dependencies(command)
    context = deepcopy(adapter._acquisition_context)
    pending = set(getattr(adapter, "_pending_control_keys", set()))
    adapter._selective_control = complete and keys is not None
    try:
        result = adapter._apply_command(command)
        if result.outcome != "success":
            adapter.invalidate_acquisition_context("error")
            return result
        if command.operation == "auto_setup" or (complete and keys is None):
            adapter.snapshot()
            adapter._ranges_dirty = True
            adapter.refresh_capabilities()
            keys = {split(key) for key in adapter._settings}
            context = None
        elif not complete:
            return result  # Legacy/partial configuration keeps the conservative path.
        else:
            assert keys is not None
            verified = {(command.channel, command.operation)}
            if command.operation == "trigger_source":
                verified |= {(1, "trigger_source"), (1, "trigger_setup")}
            elif command.operation in {"run", "stop", "single"}:
                verified.add((1, "trigger_mode"))
            for ch, name in sorted(keys - verified):
                adapter.read(name, ch)
            update_ranges(adapter, command, keys)
        assert keys is not None
        setup = adapter._settings.get("1.trigger_setup")
        if setup:
            adapter._settings["1.trigger_source"] = trigger_setup(setup.encode("ascii"))["SR"]
            adapter._stamps["1.trigger_source"] = adapter._stamps["1.trigger_setup"]
        if context:
            for ch, name in keys:
                key = f"{ch}.{name}"
                if key in context.configuration:
                    context.configuration[key] = adapter._settings[key]
                    context.timestamps[key] = adapter._stamps[key]
            op = command.operation
            for ch, fields in context.descriptors.items():
                allowed = set()
                if op in VERTICAL and ch == command.channel:
                    allowed = (
                        {"offset", "acquisition_vertical_offset"}
                        if op == "offset"
                        else VERTICAL_FIELDS | {"coupling_code", "probe_attenuation"}
                    )
                elif op in HORIZONTAL | {"channel_enabled"}:
                    allowed = HORIZONTAL_FIELDS
                for name in allowed:
                    fields.pop(name, None)
            adapter._acquisition_context = context
            adapter._pending_control_keys = pending | {
                k for k in keys if f"{k[0]}.{k[1]}" in context.configuration
            }
        readings = tuple(
            ParameterReading(
                name, ch, value, "instrument" if (ch, name) in keys else "control_cache", adapter._stamps[key]
            )
            for key, value in adapter._settings.items()
            for ch, name in (split(key),)
            if key in adapter._stamps
        )
        return replace(result, control_update=ControlUpdate(readings, tuple(sorted(keys)), True))
    except ValidationError:
        adapter.invalidate_acquisition_context("invalid_parameter")
        raise
    except Exception:
        adapter.invalidate_acquisition_context("error")
        raise
    finally:
        adapter._selective_control = False


def split(key: str) -> tuple[int, str]:
    channel, name = key.split(".", 1)
    return int(channel), name
