"""Offline conversion, including early identity-only and rejected-query evidence."""

from copy import deepcopy
from pathlib import Path
from typing import Any

from lab_control.storage.hardware_records import matches, normalize

ADAPTER_ID = "lecroy.9404m"


def recognizes(report: dict[str, Any], folder: Path) -> bool:
    return matches(report, folder, ADAPTER_ID, ("WAVERUNNER9404M", "9404M"))


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    source = deepcopy(report)
    # Early discovery diagnostics stored raw replies before a strict parser existed.
    # A completed exchange carrying a warning is not a successful parameter read.
    warnings = [op for op in source.get("operations", []) if str(op.get("reply", "")).startswith("WARNING")]
    if warnings:
        source.setdefault("stages", {})["parameter_read"] = {
            "status": "blocked",
            "reason": "Instrument rejected queries: remote interface mismatch",
            "original_result": report.get("stages", {}).get("parameter_read"),
        }
        source["notes"] = (
            "Raw evidence preserved; completed warning replies do not verify instrument settings."
        )
    return normalize(
        source,
        adapter_id=ADAPTER_ID,
        model="WaveRunner9404M",
        stage_names={},
        expected_models=("WaveRunner9404M", "9404M"),
    )


def publish_report(folder: Path) -> None:
    from lab_control.storage.hardware_records import publish_report as publish

    publish(folder, summarize)
