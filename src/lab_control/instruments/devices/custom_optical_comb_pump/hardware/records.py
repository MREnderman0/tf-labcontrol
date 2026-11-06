"""Offline normalization of future pump hardware test reports."""

from pathlib import Path
from typing import Any

from lab_control.storage.hardware_records import matches, normalize

from ..adapter import ADAPTER_ID, MODEL


def recognizes(report: dict[str, Any], folder: Path) -> bool:
    return matches(report, folder, ADAPTER_ID, (MODEL,))


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    return normalize(report, adapter_id=ADAPTER_ID, model=MODEL, stage_names={})
