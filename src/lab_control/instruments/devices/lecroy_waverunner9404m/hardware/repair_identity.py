"""Pure, narrowly scoped repair plan for the historical *IDN manufacturer bug.

The caller must back up records, acquire exclusive maintenance ownership, obtain
a fresh full identity through the normal asset lease, and commit atomically.
No hardware, database, user settings or history are modified by this function.
"""

from copy import deepcopy

from lab_control.domain.instruments import DeviceError, DeviceIdentity, jsonable
from lab_control.instruments.base import identity_from_scpi


def plan(records: list[dict], verified: DeviceIdentity) -> tuple[dict, tuple[str, ...]]:
    if verified.model != "WAVERUNNER9404M" or not verified.key or not records:
        raise DeviceError("Require the freshly verified complete target identity", "identity_required")
    corrected = []
    legacy = False
    for record in records:
        identity = DeviceIdentity(**record["identity"])
        parsed = identity_from_scpi(identity.raw.encode("ascii"))
        if (
            parsed != verified
            or identity.model != verified.model
            or identity.serial != verified.serial
            or identity.firmware != verified.firmware
            or record.get("identityKey") != identity.key
            or identity.manufacturer not in (verified.manufacturer, "*IDN " + verified.manufacturer)
        ):
            raise DeviceError("Saved identity is not the proven response-header alias", "identity_conflict")
        if record.get("adapterId") != "lecroy.9404m" or record["config"].get("adapter") != "lecroy.9404m":
            raise DeviceError("Adapter differs", "identity_conflict")
        if record["config"].get("expected_serial") not in ("", verified.serial):
            raise DeviceError("Configured serial differs", "identity_conflict")
        conflict = record.get("identityConflict")
        if conflict and conflict != jsonable(verified):
            raise DeviceError("Conflict is not the matching headerless identity", "identity_conflict")
        legacy |= identity.manufacturer.startswith("*IDN ")
        corrected.append(record)
    if not legacy:
        raise DeviceError("No legacy response-header record to repair", "unsupported")
    # Keep the original canonical asset ID and its settings; no running sessions
    # may exist when the caller applies this plan.
    winner = deepcopy(
        next((r for r in corrected if r["identity"]["manufacturer"] == verified.manufacturer), corrected[0])
    )
    resource = winner["config"]["resource"]
    names = {r.get("displayName", "") for r in corrected} - {""}
    slots: dict[str, str] = {}
    for record in corrected:
        if (
            record["config"] != winner["config"]
            or set(record["resources"]) != {resource}
            or set(record["candidates"]) != {resource}
            or record.get("primaryResourceExplicit") != winner.get("primaryResourceExplicit")
        ):
            raise DeviceError(
                "Connection preferences differ; do not silently merge them", "identity_conflict"
            )
        candidate = record["candidates"][resource]
        if candidate.get("verifiedIdentityKey") not in (record["identityKey"], verified.key):
            raise DeviceError("Candidate belongs to a different identity", "identity_conflict")
        for mode, slot in record.get("slots", {}).items():
            if slot and mode in slots and slots[mode] != slot:
                raise DeviceError("Function slot assignments differ", "slot_conflict")
            if slot:
                slots[mode] = slot
    if len(names) > 1:
        raise DeviceError("Display names differ; preserve both records for review", "identity_conflict")
    newest = max(corrected, key=lambda r: r.get("lastSeenAt", ""))
    winner["candidates"][resource] = deepcopy(newest["candidates"][resource])
    winner["candidates"][resource].update(
        verifiedIdentityKey=verified.key,
        retired=False,
        presenceState="unknown",
        probeReason="identity_header_repaired",
    )
    winner["candidates"][resource].pop("evidenceScanRunId", None)
    winner.update(
        identity=jsonable(verified),
        identityKey=verified.key,
        discoveryState="unknown",
        presenceState="unknown",
        probeReason="identity_header_repaired",
        lastError="",
        slots=slots,
        displayName=next(iter(names), ""),
    )
    winner.pop("identityConflict", None)
    return winner, tuple(r["id"] for r in corrected if r["id"] != winner["id"])
