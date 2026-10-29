from pathlib import Path
from typing import Any

from lab_control.storage.hardware_records import matches, normalize

ADAPTER_ID = "rigol.m300"


def recognizes(report: dict[str, Any], folder: Path) -> bool:
    return matches(report, folder, ADAPTER_ID, ("RIGOL M300", "M300"))


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    # The thermistor entry preserves a structured revision and model file hashes.
    # Map that model-owned format without changing historical original reports.
    mapped = dict(report)
    if not mapped.get("project_head") and (report.get("source_version") or report.get("model_source_hashes")):
        mapped["project_head"] = {
            "revision": report.get("source_version", {}),
            "model_source_hashes": report.get("model_source_hashes", {}),
        }
    return normalize(
        mapped,
        adapter_id=ADAPTER_ID,
        model="M300",
        stage_names={"restoration": "recovery", "reconnection": "reconnect"},
        expected_models=("M300",),
    )


def publish_report(folder: Path) -> None:
    from lab_control.storage.hardware_records import publish_report as publish

    publish(folder, summarize)
