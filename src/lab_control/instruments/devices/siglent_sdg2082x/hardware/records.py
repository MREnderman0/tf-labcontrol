"""Offline record interpretation owned by SIGLENT SDG2082X; no instrument commands."""

from pathlib import Path
from typing import Any

from lab_control.storage.hardware_records import matches, normalize

ADAPTER_ID = "siglent.sdg2082x"
MODEL = "SIGLENT SDG2082X"
STAGE_NAMES = {"repeat_control": "repetition", "repeat_read": "stable_reads"}


def recognizes(report: dict[str, Any], folder: Path) -> bool:
    return matches(report, folder, ADAPTER_ID, ("SDG2082X",))


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    result = normalize(
        report, adapter_id=ADAPTER_ID, model=MODEL, stage_names=STAGE_NAMES, expected_models=("SDG2082X",)
    )
    for item in result["items"]:
        if item["key"] == "arbitrary" and report.get("arbitrary_reason"):
            item["reason"] = str(report["arbitrary_reason"])
    period = report.get("burst_period_test")
    if (
        isinstance(period, dict)
        and period.get("status") == "not_tested"
        and not any(item["key"] == "burst_period" for item in result["items"])
    ):
        result["items"].append(
            {
                "key": "burst_period",
                "source_key": "burst_period_test",
                "status": "not_tested",
                "raw_result": period,
                "reason": str(period.get("reason", "")),
            }
        )
    return result


def publish_report(folder: Path) -> None:
    from lab_control.storage.hardware_records import publish_report as save_summary

    save_summary(folder, summarize)
