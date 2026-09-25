"""Current resource evidence is independent of saved identity and connection state."""

from __future__ import annotations

from typing import Any

LIVE_CONNECTIONS = {"connected", "busy", "degraded"}


def is_current(record: dict[str, Any], session: dict[str, Any] | None = None) -> bool:
    if (session or {}).get("connectionState") in LIVE_CONNECTIONS | {"connecting"}:
        return True
    if record.get("presenceState") == "present":
        return True
    # A fresh physical announcement remains visible when identity/open fails.
    # Saved configured addresses and previous-process evidence are not sightings.
    return record.get("presenceState") == "unreachable" and any(
        not candidate.get("retired")
        and any(
            observation.get("scanRunId") == record.get("scanRunId") and local_enumeration(source, resource)
            for source, observation in candidate.get("sources", {}).items()
        )
        for resource, candidate in record.get("candidates", {}).items()
    )


def presence_label(record: dict[str, Any], session: dict[str, Any] | None = None) -> str:
    if (session or {}).get("connectionState") in LIVE_CONNECTIONS:
        return "当前会话"
    return {
        "present": "本次发现",
        "not_seen": "本次未发现",
        "unreachable": "探测失败",
        "unknown": "尚未确认",
    }.get(record.get("presenceState", "unknown"), "尚未确认")


def local_enumeration(source: str, resource: str) -> bool:
    # VISA can retain configured LAN/GPIB addresses even with no responding instrument.
    return (
        source in {"serial", "lan"}
        or source.startswith("sdk:")
        or (source == "visa" and resource.strip().upper().startswith(("USB", "ASRL", "PXI")))
    )


def update_record_presence(record: dict[str, Any]) -> None:
    states = {c.get("presenceState", "unknown") for c in record["candidates"].values()}
    presence = next((s for s in ("present", "unreachable", "unknown", "not_seen") if s in states), "unknown")
    record["presenceState"] = presence
    if record.get("discoveryState") == "conflict" or record.get("identityConflict"):
        record["discoveryState"] = "conflict"
    elif presence == "present":
        record["discoveryState"] = "identified" if record["identity"] else "unknown"
    else:
        record["discoveryState"] = {"not_seen": "stale", "unreachable": "unreachable"}.get(
            presence, "unknown"
        )
