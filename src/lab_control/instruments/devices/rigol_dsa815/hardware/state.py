"""Verification-only marker backup commands, kept out of normal page editors."""

from dataclasses import replace

from ..profile import dsa815

MARKER_FIELDS = (
    "enabled",
    "mode",
    "position",
    "frequency",
    "readout",
    "trace",
    "trace_auto",
    "function",
    "tracking",
    "search",
)
OTHER_MARKERS = tuple(f"marker{n}_{field}" for n in (2, 3, 4) for field in MARKER_FIELDS)


def verification_profile():
    profile = dsa815()
    commands = dict(profile.commands)
    for n in (2, 3, 4):
        for field in MARKER_FIELDS:
            original = commands["marker_" + field]
            name = f"marker{n}_{field}"
            # Other markers are restored by integer screen position, never by a drifting noise peak.
            commands[name] = replace(
                original,
                capability=replace(
                    original.capability,
                    name=name,
                    writable=field in {"enabled", "mode", "position", "readout"},
                ),
                writes=tuple(w.replace("MARK1", f"MARK{n}") for w in original.writes)
                if field in {"enabled", "mode", "position", "readout"}
                else (),
                query=original.query.replace("MARK1", f"MARK{n}"),
            )
    return replace(profile, commands=commands)
