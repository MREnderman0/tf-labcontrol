"""Offline record conversion for K+K FXE compatibility batches."""

from pathlib import Path
from typing import Any

from lab_control.storage.hardware_records import matches, normalize

ADAPTER_ID = "kplusk.fxe"


def recognizes(report: dict[str, Any], folder: Path) -> bool:
    return matches(report, folder, ADAPTER_ID, ("K+K FXE", "K+K FXE (完整变体待核对)"))


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    summary = normalize(
        report,
        adapter_id=ADAPTER_ID,
        model="K+K FXE (完整变体待核对)",
        stage_names={
            "enumeration": "enumeration",
            "identity": "identity",
            "official_protocol_connection": "official_protocol_connection",
            "compatibility_connection": "compatibility_connection",
            "parameter_read": "parameter_read",
            "frequency_read": "frequency_read",
            "monitoring": "monitoring",
            "recording": "recording",
            "display": "display",
            "channel_selection": "channel_selection",
            "snapshot_export": "snapshot_export",
            "parameter_write": "parameter_write",
            "restoration": "restoration",
            "reconnection": "reconnection",
            "physical_output": "physical_output",
        },
        expected_models=("FXE", "K+K FXE", "K+K FXE (完整变体待核对)"),
    )
    # The project resource grammar accepts a bare K+K IP. Preserve that
    # operator-facing value while recording the verified physical interface.
    resource = str(report.get("resource", ""))
    if resource.upper().startswith("USB"):
        summary["interface"] = "USB"
        summary["protocol"] = "K+K official USB"
    else:
        summary["interface"] = "LAN"
        summary["protocol"] = "K+K direct Ethernet via switch"
    return summary


def publish_report(folder: Path) -> None:
    from lab_control.storage.hardware_documents import refresh_after_batch
    from lab_control.storage.hardware_records import publish_report as publish

    publish(folder, summarize)
    refresh_after_batch(folder)
