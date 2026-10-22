from pathlib import Path
from typing import Any

from lab_control.storage.hardware_records import matches, normalize

ADAPTER_ID = "pico.6404e"


def recognizes(report: dict[str, Any], folder: Path) -> bool:
    return matches(report, folder, ADAPTER_ID, ("6404E",))


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    return normalize(report, adapter_id=ADAPTER_ID, model="6404E", stage_names={}, expected_models=("6404E",))


def publish_report(folder: Path) -> None:
    from lab_control.storage.hardware_records import publish_report as publish

    publish(folder, summarize)
