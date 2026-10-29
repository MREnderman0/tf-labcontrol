"""Model-owned offline record interpretation; no hardware imports."""

from pathlib import Path
from typing import Any

from lab_control.storage.hardware_records import matches, normalize

ADAPTER_ID = "keysight.53220a"


def recognizes(report: dict[str, Any], folder: Path) -> bool:
    return matches(report, folder, ADAPTER_ID, ("53220A",))


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    return normalize(
        {
            **report,
            "project_head": report.get("source_version", {}),
            "source_sha256": report.get("model_source_hashes", {}),
        },
        adapter_id=ADAPTER_ID,
        model="53220A",
        stage_names={},
        expected_models=("53220A",),
    )


def publish_report(folder: Path) -> None:
    from lab_control.storage.hardware_records import publish_report as publish

    publish(folder, summarize)
