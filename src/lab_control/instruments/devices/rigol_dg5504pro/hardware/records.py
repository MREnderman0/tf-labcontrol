"""Offline record interpretation owned by RIGOL DG5504 Pro; no instrument commands."""

from pathlib import Path
from typing import Any

from lab_control.storage.hardware_records import matches, normalize

ADAPTER_ID = "rigol.dg5504pro"
MODEL = "RIGOL DG5504 Pro"
STAGE_NAMES = {"four_channel_reads": "parameter_read", "reconnect": "reconnection"}


def recognizes(report: dict[str, Any], folder: Path) -> bool:
    return matches(report, folder, ADAPTER_ID, ("DG5504Pro", "DG5504 Pro"))


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    return normalize(
        report, adapter_id=ADAPTER_ID, model=MODEL, stage_names=STAGE_NAMES, expected_models=("DG5504 Pro",)
    )


def publish_report(folder: Path) -> None:
    from lab_control.storage.hardware_records import publish_report as save_summary

    save_summary(folder, summarize)
