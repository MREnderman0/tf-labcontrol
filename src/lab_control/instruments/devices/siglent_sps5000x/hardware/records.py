from pathlib import Path
from typing import Any

from lab_control.storage.hardware_records import matches, normalize

from ..adapter import ADAPTER_ID, RATINGS


def recognizes(report: dict[str, Any], folder: Path) -> bool:
    return matches(report, folder, ADAPTER_ID, tuple(RATINGS))


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    expected = str(report.get("expected_model", "")).upper()
    summary = normalize(
        report,
        adapter_id=ADAPTER_ID,
        model=expected if expected in RATINGS else "SPS5000X",
        stage_names={"scan": "enumeration", "connection": "compatibility_connection"},
        expected_models=(expected,) if expected else tuple(RATINGS),
    )
    if report.get("transport") == "tcp":
        summary["protocol"] = "TCP Socket"
    return summary


def publish_report(folder: Path) -> None:
    from lab_control.storage.hardware_records import publish_report as publish

    publish(folder, summarize)
