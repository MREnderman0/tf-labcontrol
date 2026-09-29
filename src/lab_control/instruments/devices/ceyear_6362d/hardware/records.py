from pathlib import Path
from typing import Any

from lab_control.storage.hardware_records import matches, normalize

ADAPTER_ID = "ceyear.6362d"


def recognizes(report: dict[str, Any], folder: Path) -> bool:
    return matches(report, folder, ADAPTER_ID, ("6362D", "CY6362D"))


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    return normalize(
        report, adapter_id=ADAPTER_ID, model="6362D", stage_names={}, expected_models=("6362D", "CY6362D")
    )


def publish_report(folder: Path) -> None:
    from lab_control.storage.hardware_documents import refresh_after_batch
    from lab_control.storage.hardware_records import publish_report as publish

    publish(folder, summarize)
    refresh_after_batch(folder)
