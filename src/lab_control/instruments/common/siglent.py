"""Shared SIGLENT composite-response field parsing."""

from typing import Any

from lab_control.domain.instruments import ValidationError


def sdg_field(raw: bytes, field: str) -> Any:
    text = raw.decode("ascii").strip()
    parts = text.split(" ", 1)[-1].split(",")
    for index, item in enumerate(parts[:-1]):
        if item.upper() == field:
            value = parts[index + 1]
            for suffix in ("HZ", "V", "S", "%"):
                if value.upper().endswith(suffix):
                    value = value[: -len(suffix)]
                    break
            try:
                return float(value)
            except ValueError:
                return value
    raise ValidationError(f"Missing {field} in SDG response")
