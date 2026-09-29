from pathlib import Path
from typing import Any

from lab_control.storage.hardware_records import matches, normalize


def recognizes(report: dict[str, Any], folder: Path) -> bool:
    return matches(report, folder, "microchip.53100a", ("53100A",))


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    return normalize(
        report, adapter_id="microchip.53100a", model="53100A", stage_names={}, expected_models=("53100A",)
    )


def publish_report(folder: Path) -> None:
    from lab_control.storage.hardware_documents import refresh_after_batch
    from lab_control.storage.hardware_records import publish_report as publish

    publish(folder, summarize)
    refresh_after_batch(folder)
