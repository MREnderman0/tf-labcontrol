"""DHO924 evidence conversion, separate from control and raw artifacts."""

from pathlib import Path

from lab_control.storage.hardware_records import matches, normalize
from lab_control.storage.hardware_records import publish_report as publish


def recognizes(report: dict, folder: Path) -> bool:
    return matches(report, folder, "rigol.dho924", ("DHO924",))


def summarize(report: dict) -> dict:
    return normalize(
        report, adapter_id="rigol.dho924", model="DHO924", stage_names={}, expected_models=("DHO924",)
    )


def publish_report(folder: Path) -> None:
    publish(folder, summarize)
