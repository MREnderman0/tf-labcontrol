from pathlib import Path
from typing import Any

from lab_control.storage.hardware_records import matches, normalize

ADAPTER_ID = "lecroy.9254"


def recognizes(report: dict[str, Any], folder: Path) -> bool:
    return matches(report, folder, ADAPTER_ID, ("WaveRunner9254", "9254"))


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    return normalize(
        report,
        adapter_id=ADAPTER_ID,
        model="WaveRunner9254",
        stage_names={},
        expected_models=("WaveRunner9254", "9254"),
    )


def publish_report(folder: Path) -> None:
    from lab_control.storage.hardware_records import publish_report as publish

    publish(folder, summarize)
