"""Explicit query-only identity/status/reconnection verification."""

from lab_control.instruments.common.readonly_hardware import main
from lab_control.instruments.devices.siglent_sdg6032xe import DEFINITION

from .records import publish_report

if __name__ == "__main__":
    raise SystemExit(main(DEFINITION, record_summary=publish_report))
