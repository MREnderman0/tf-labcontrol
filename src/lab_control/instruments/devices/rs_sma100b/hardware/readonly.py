"""Read-only SMA100B verification, including its verified range queries."""

from lab_control.instruments.common.readonly_hardware import main
from lab_control.instruments.devices.rs_sma100b import DEFINITION

from .records import publish_report

assert DEFINITION.profile is not None

EXTRA_QUERIES = (
    "*OPT?",
    "SOUR:POW:OFFS?",
    "SOUR:POW:LIM?",
    *(
        DEFINITION.profile.commands[name].query + " " + limit
        for name in ("frequency", "sweep_start", "sweep_stop", "power")
        for limit in ("MIN", "MAX")
    ),
)

if __name__ == "__main__":
    raise SystemExit(main(DEFINITION, EXTRA_QUERIES, record_summary=publish_report))
