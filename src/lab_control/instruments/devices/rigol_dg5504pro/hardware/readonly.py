"""DG5504 Pro identity, basic state and output read-only verification."""

from lab_control.instruments.common.readonly_hardware import main
from lab_control.instruments.devices.rigol_dg5504pro import DEFINITION

from .records import publish_report

EXTRA_QUERIES = tuple(f"OUTP{channel}?" for channel in range(1, 5))

if __name__ == "__main__":
    raise SystemExit(main(DEFINITION, EXTRA_QUERIES, record_summary=publish_report))
