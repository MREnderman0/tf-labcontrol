"""Offline record conversion for the CoreMorrow XMT package."""

from pathlib import Path
from typing import Any

from lab_control.storage.hardware_records import matches, normalize

ADAPTER_ID = "coremorrow.xmt"


def recognizes(report: dict[str, Any], folder: Path) -> bool:
    return matches(report, folder, ADAPTER_ID, ("XMT-compatible controller (exact model pending)", "CoreMorrow XMT"))


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    return normalize(
        report,
        adapter_id=ADAPTER_ID,
        model="XMT-compatible controller (exact model pending)",
        stage_names={},
        # The adapter remains an XMT-family implementation.  A hardware
        # batch may carry a manually observed exact nameplate model while the
        # protocol identity is still provisional.
        expected_models=("XMT-compatible controller (exact model pending)", "E00.C3-13"),
    )


def publish_report(folder: Path) -> None:
    from lab_control.storage.hardware_documents import refresh_after_batch
    from lab_control.storage.hardware_records import publish_report as publish

    publish(folder, summarize)
    refresh_after_batch(folder)

