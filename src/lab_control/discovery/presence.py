"""Current resource evidence is independent of saved identity and connection state."""

from __future__ import annotations

from typing import Any

LIVE_CONNECTIONS = {"connected", "busy", "degraded", "recovering"}


def is_current(record: dict[str, Any], session: dict[str, Any] | None = None) -> bool:
    if (session or {}).get("connectionState") in LIVE_CONNECTIONS | {"connecting"}:
        return True
    # A failed identity/open probe is never current, even when the scan
    # enumerated a local bridge.  Enumeration proves that the bridge exists,
    # not that the configured instrument answered as this device.  Keep failed
    # probes in history/all so their error remains actionable without
    # advertising them as devices or sending them through one-click connection
    # again.
    if record.get("presenceState") != "present":
        return False
    # A bare COM/USB resource is a repair/configuration candidate, not a
    # discovered instrument.  Keep it in history/all until a model is
    # selected or an identity probe succeeds.
    return not (
        record.get("probeReason") == "metadata_only"
        and not record.get("identity")
        and not record.get("adapterId")
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
        source in {"serial", "lan", "usb_pnp"}
        or source.startswith("sdk:")
        or (source == "visa" and resource.strip().upper().startswith(("USB", "ASRL", "PXI")))
    )


def update_record_presence(record: dict[str, Any]) -> None:
    candidates = [candidate for candidate in record["candidates"].values() if not candidate.get("retired")]
    # Only evidence from the latest scan can establish the record's current
    # state.  A serial port often has a matching VISA ASRL alias; that alias
    # is published before probing and must not turn a timed-out known model
    # back into a current device.
    current_scan = record.get("scanRunId")
    current = [candidate for candidate in candidates if candidate.get("scanRunId") == current_scan]
    evidence = current or candidates
    successful_probe = any(
        candidate.get("presenceState") == "present"
        and candidate.get("probeReason") in {"identified", "cached_active_session"}
        for candidate in evidence
    )
    states = {candidate.get("presenceState", "unknown") for candidate in evidence}
    if successful_probe:
        presence = "present"
    else:
        presence = next(
            (state for state in ("unreachable", "present", "unknown", "not_seen") if state in states),
            "unknown",
        )
    record["presenceState"] = presence
    if record.get("discoveryState") == "conflict" or record.get("identityConflict"):
        record["discoveryState"] = "conflict"
    elif presence == "present":
        record["discoveryState"] = "identified" if record["identity"] else "unknown"
    else:
        record["discoveryState"] = {"not_seen": "stale", "unreachable": "unreachable"}.get(
            presence, "unknown"
        )
