"""Read-only SDG2082X verification; no parameter/output writes."""

from lab_control.instruments.common.readonly_hardware import main
from lab_control.instruments.devices.siglent_sdg2082x import DEFINITION

if __name__ == "__main__":
    raise SystemExit(main(DEFINITION))
