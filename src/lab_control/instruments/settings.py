"""Readback helpers that never disguise accepted settings as instrument measurements."""

from typing import Any

from lab_control.domain.instruments import ParameterReading, Quality


def cached_reading(operation: str, channel: int, values: dict[str, Any]) -> ParameterReading:
    value = values.get(f"{channel}.{operation}", values.get(operation))
    return ParameterReading(
        operation,
        channel,
        value,
        "session_cache" if value is not None else "unavailable",
        quality=Quality.GOOD if value is not None else Quality.STALE,
    )
