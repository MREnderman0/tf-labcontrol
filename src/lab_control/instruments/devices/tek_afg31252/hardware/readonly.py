"""Read-only AFG31252 post-migration verification; explicit asset config required."""

from lab_control.instruments.common.readonly_hardware import main
from lab_control.instruments.devices.tek_afg31252 import DEFINITION

if __name__ == "__main__":
    raise SystemExit(main(DEFINITION))
