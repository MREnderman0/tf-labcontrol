"""Query-only entry point using the same selected-asset runner and records."""

import sys

from .validate_control import main

if __name__ == "__main__":
    if any(arg.split("=", 1)[0] in {"--control-unloaded", "--final-ch4-current"} for arg in sys.argv[1:]):
        raise SystemExit("Use validate_control for explicitly authorized setting/output tests")
    raise SystemExit(main())
