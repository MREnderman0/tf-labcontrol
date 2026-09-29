"""Read-only model entry; login permitted, measurement/format writes disabled."""

import sys

from .validate_control import main

if __name__ == "__main__":
    if "--control" in sys.argv or "--qualify-transfer" in sys.argv:
        raise SystemExit("Use validate_control for explicitly authorized write validation")
    raise SystemExit(main())
