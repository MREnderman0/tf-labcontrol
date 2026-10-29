"""Add a general workspace without deleting legacy preferences or recordings."""

from __future__ import annotations

import copy
import json
from collections import defaultdict

from lab_control.domain.workspace import GENERAL, LEGACY_MODES, category_slots

MIGRATION_KEY = "workspace_migration/general-v1"
DEVICE_PREFIXES = ("draft", "monitor-timing", "spectrum-markers", "scope-fine")


def migrate_general_workspace(repository, category_for) -> dict:
    """Atomically copy existing settings; explicit null/empty values are choices.

    Run after the runtime process lease, before Inventory loads its in-memory
    records. This function performs no discovery, instrument I/O or file moves.
    """
    with repository.lock, repository.db:
        saved = dict(repository.db.execute("SELECT id, document FROM workspace"))
        if MIGRATION_KEY in saved:
            return json.loads(saved[MIGRATION_KEY])
        workspace = {key: json.loads(value) for key, value in saved.items()}
        original = copy.deepcopy(workspace)
        records = repository.records()
        previous_mode = workspace.get("current_mode", "electronic")
        report = {"version": 1, "previous_mode": previous_mode, "slot_conflicts": {}, "copied_keys": []}

        def preferred(record):
            category = category_for(record)
            first = (
                "optical"
                if category in {"optical_power_meter", "optical_spectrometer"}
                else "electronic"
                if category == "spectrum_analyzer"
                else previous_mode
                if previous_mode in LEGACY_MODES
                else "electronic"
            )
            return (first, next(mode for mode in LEGACY_MODES if mode != first))

        def copy_first(target, candidates):
            if target not in workspace:
                for key in candidates:
                    if key in workspace:
                        workspace[target] = copy.deepcopy(workspace[key])
                        report["copied_keys"].append(target)
                        break

        proposals = {}
        owners = defaultdict(list)
        changed = []
        for record in records:
            slots = record.get("slots", {})
            key = record["id"]
            if GENERAL in slots:
                if slots[GENERAL]:
                    owners[slots[GENERAL]].append(key)
                continue
            choices = {slots[mode] for mode in LEGACY_MODES if mode in slots}
            category = category_for(record)
            valid_slots = set(category_slots(category))
            # Even an internally conflicting device still claims its old
            # slots: do not silently assign either to another device.
            for slot in choices & valid_slots:
                owners[slot].append(key)
            if len(choices) > 1:
                report["slot_conflicts"][key] = "旧模式的槽位绑定不一致，请重新绑定"
            elif choices:
                slot = next(iter(choices))
                if slot and slot not in valid_slots:
                    report["slot_conflicts"][key] = "旧槽位与设备类别不匹配，请重新绑定"
                else:
                    proposals[key] = slot
        for slot, keys in owners.items():
            if len(keys) > 1:
                for key in keys:
                    if key in proposals:
                        report["slot_conflicts"][key] = "多台设备使用同一旧槽位，请重新绑定"

        for record in records:
            device = record["id"]
            modes = preferred(record)
            slots = record.setdefault("slots", {})
            if GENERAL not in slots:
                slots[GENERAL] = "" if device in report["slot_conflicts"] else proposals.get(device, "")
                if device in report["slot_conflicts"]:
                    record["slotMigrationConflict"] = report["slot_conflicts"][device]
                changed.append(record)
            for prefix in DEVICE_PREFIXES:
                suffixes = {
                    key.split("/", 2)[2]
                    for key in original
                    if any(
                        key.startswith(f"{prefix}/{mode}/{device}") and key.split("/", 3)[2] == device
                        for mode in LEGACY_MODES
                    )
                }
                for suffix in suffixes:
                    copy_first(
                        f"{prefix}/{GENERAL}/{suffix}", [f"{prefix}/{mode}/{suffix}" for mode in modes]
                    )

            # Dictionaries have per-slot keys rather than per-mode top-level
            # keys. Keep every old key and add only the new general target.
            for container in ("monitor_overview/all", "monitoring_channels/optical_power_meter"):
                values = workspace.get(container, {})
                if not isinstance(values, dict):
                    continue  # An explicit cleared/malformed container stays intact.
                target = f"slot:{GENERAL}:{slots[GENERAL]}" if slots[GENERAL] else f"device:{device}"
                if target in values:
                    continue
                found = False
                for mode in modes:
                    slot = slots.get(mode, "")
                    candidates = [f"slot:{mode}:{slot}"] if slot else []
                    candidates += [f"device:{device}", device]
                    sources = [(values, candidates)]
                    if container == "monitor_overview/all":
                        legacy = workspace.get(f"monitor_overview/{mode}", {})
                        sources.append((legacy, candidates + ([f"slot:{slot}", slot] if slot else [])))
                    for source, keys in sources:
                        if not isinstance(source, dict):
                            continue
                        for key in keys:
                            if key in source:
                                values[target] = copy.deepcopy(source[key])
                                found = True
                                break
                        if found:
                            break
                    if found:
                        workspace[container] = values
                        break

        workspace["current_mode"] = GENERAL
        workspace[MIGRATION_KEY] = report
        repository.db.executemany(
            "INSERT OR REPLACE INTO inventory VALUES (?, ?)",
            [(record["id"], json.dumps(record, ensure_ascii=False, allow_nan=False)) for record in changed],
        )
        repository.db.executemany(
            "INSERT OR REPLACE INTO workspace VALUES (?, ?)",
            [
                (key, json.dumps(value, ensure_ascii=False, allow_nan=False))
                for key, value in workspace.items()
                if key not in original or original[key] != value
            ],
        )
        return report
