"""Offline JW8105 envelope; candidate metadata never proves instrument identity."""

from pathlib import Path
from typing import Any

from lab_control.storage.hardware_records import matches, normalize

ADAPTER_ID = "joinwit.jw8105"


def recognizes(report: dict[str, Any], folder: Path) -> bool:
    return matches(report, folder, ADAPTER_ID, ("JW8105", "Joinwit-JW8105"))


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    return normalize(report, adapter_id=ADAPTER_ID, model="JW8105", stage_names={}, expected_models=("JW8105",))


def publish_report(folder: Path) -> None:
    from lab_control.storage.hardware_documents import refresh_after_batch
    from lab_control.storage.hardware_records import publish_report as publish

    publish(folder, summarize)
    refresh_after_batch(folder)
