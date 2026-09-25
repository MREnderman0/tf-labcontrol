"""Offline record interpretation owned by Tektronix AFG31252; no instrument commands."""

from pathlib import Path
from typing import Any

from lab_control.storage.hardware_records import matches, normalize

ADAPTER_ID = "tek.afg31252"
MODEL = "Tektronix AFG31252"
STAGE_NAMES = {
    "compatibility_connection": "connection",
    "repeat_control": "repetition",
    "repeat_read": "stable_reads",
}


def recognizes(report: dict[str, Any], folder: Path) -> bool:
    return matches(report, folder, ADAPTER_ID, ("AFG31252",))


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    return normalize(
        report, adapter_id=ADAPTER_ID, model=MODEL, stage_names=STAGE_NAMES, expected_models=("AFG31252",)
    )


def publish_report(folder: Path) -> None:
    from lab_control.storage.hardware_records import publish_report as save_summary

    save_summary(folder, summarize)
