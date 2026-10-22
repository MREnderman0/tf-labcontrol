from pathlib import Path
from typing import Any

from lab_control.storage.hardware_records import matches, normalize

ADAPTER_ID = "keithley.2231a"


def recognizes(report: dict[str, Any], folder: Path) -> bool:
    return matches(report, folder, ADAPTER_ID, ("2231A-30-3",))


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    return normalize(
        report, adapter_id=ADAPTER_ID, model="2231A-30-3", stage_names={}, expected_models=("2231A-30-3",)
    )


def publish_report(folder: Path) -> None:
    from lab_control.storage.hardware_records import publish_report as publish

    publish(folder, summarize)
