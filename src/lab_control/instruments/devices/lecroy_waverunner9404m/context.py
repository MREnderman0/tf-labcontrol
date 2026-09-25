"""Per-run configuration provenance; no samples or transport handles are cached."""

from dataclasses import dataclass
from typing import Any

from lab_control.domain.instruments import Measurement

# Deliberately exclude trigger time and horizontal origin: both can change on
# every sweep. Enum codes are compared opaquely, not interpreted as legacy ranges.
FINGERPRINT_FIELDS = (
    "template",
    "sample_width",
    "byte_order",
    "raw_point_count",
    "first_valid",
    "last_valid",
    "first_point",
    "sparsing_factor",
    "gain",
    "offset",
    "horizontal_interval",
    "timebase_code",
    "coupling_code",
    "probe_attenuation",
    "vertical_gain_code",
    "bandwidth_limit_code",
    "vertical_vernier",
    "acquisition_vertical_offset",
    "processing_code",
)


def fingerprint(measurement: Measurement) -> dict[str, Any]:
    return {
        **{key: measurement.metadata[key] for key in FINGERPRINT_FIELDS},
        "unit": measurement.unit,
        "axis_unit": measurement.axis_unit,
    }


@dataclass
class AcquisitionContext:
    key: tuple[str, str, tuple[int, ...]]
    configuration: dict[str, Any]
    timestamps: dict[str, str]
    descriptors: dict[int, dict[str, Any]]
    tokens: dict[int, str | None]
