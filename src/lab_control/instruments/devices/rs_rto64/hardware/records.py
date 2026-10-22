from pathlib import Path
from typing import Any

from lab_control.storage.hardware_records import matches, normalize

ADAPTER_ID = "rs.rto64"


def recognizes(report: dict[str, Any], folder: Path) -> bool:
    return matches(report, folder, ADAPTER_ID, ("RTO64",))


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    return normalize(report, adapter_id=ADAPTER_ID, model="RTO64", stage_names={}, expected_models=("RTO64",))


def publish_report(folder: Path) -> None:
    from lab_control.storage.hardware_records import publish_report as publish

    publish(folder, summarize)
