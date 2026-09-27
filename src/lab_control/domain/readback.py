"""Value interpretation shared by read-only synchronization and its display."""

import math
from typing import Any

from lab_control.domain.instruments import Parameter, ParameterReading, Quality

# Known display/protocol equivalents, never arbitrary prefix acceptance.
ENUM_EQUIVALENTS = (
    {"SIN", "SINE", "SINUSOID"},
    {"SQU", "SQUARE"},
    {"PULS", "PULSE"},
    {"NOIS", "NOISE"},
    {"INT", "INTERNAL"},
    {"EXT", "EXTERNAL"},
    {"LIN", "LINE", "LINEAR"},
    {"LOG", "LOGARITHMIC"},
)


def boolean_value(value: Any) -> bool | None:
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, str):
        token = value.strip().upper()
        if token in {"ON", "TRUE"}:
            return True
        if token in {"OFF", "FALSE"}:
            return False
        try:
            value = float(token)
        except ValueError:
            return None
    if isinstance(value, (bool, int, float)) and value in (0, 1):
        return bool(value)
    return None


def normalized_value(value: Any, spec: Parameter) -> Any:
    if spec.kind == "boolean":
        return boolean_value(value)
    if spec.kind == "enum":
        token = str(value).strip().upper()
        matches = [choice for choice in spec.choices if str(choice).upper() == token]
        if matches:
            return matches[0]
        equivalents = next((group for group in ENUM_EQUIVALENTS if token in group), set())
        matches = [choice for choice in spec.choices if str(choice).upper() in equivalents]
        return matches[0] if len(matches) == 1 else None
    if spec.kind in {"number", "integer"}:
        try:
            number = float(value)
            return (
                number if math.isfinite(number) and (spec.kind != "integer" or number.is_integer()) else None
            )
        except (ValueError, TypeError, OverflowError):
            return None
    return value


def usable_reading(reading: ParameterReading, parameters: dict[str, Parameter]) -> bool:
    if reading.error or reading.source not in {"instrument", "simulated", "control_cache"}:
        return False
    if reading.quality not in {Quality.GOOD, Quality.SIMULATED} or reading.value is None:
        return False
    values = reading.value if isinstance(reading.value, dict) else {"value": reading.value}
    return all(
        name in values and normalized_value(values[name], spec) is not None
        for name, spec in parameters.items()
    )
