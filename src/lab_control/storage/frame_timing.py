"""Shared CSV timing columns. Missing historical evidence remains empty."""

from typing import Any, Mapping

TIMING_COLUMNS = (
    "csv_format_version",
    "host_read_started_at",
    "host_read_completed_at",
    "read_duration_seconds",
    "recording_elapsed_seconds",
    "channel_received_at",
    "received_time_source",
    "instrument_trigger_time",
    "trigger_time_source",
    "time_basis",
)


def timing_columns(timing: Mapping[str, Any], metadata: Mapping[str, Any], time_basis: str) -> list[Any]:
    return [
        2,
        timing.get("host_read_started_at", ""),
        timing.get("host_read_completed_at", ""),
        timing.get("read_duration_seconds", ""),
        timing.get("recording_elapsed_seconds", ""),
        metadata.get("received_at", ""),
        metadata.get("received_time_source", ""),
        metadata.get("trigger_time_local") or "",
        metadata.get("trigger_time_source", ""),
        time_basis,
    ]
