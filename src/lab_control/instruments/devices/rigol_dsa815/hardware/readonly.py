"""Import-safe read-only hardware entry; no access until main is called."""

from .validate_control import main

if __name__ == "__main__":
    raise SystemExit(main())
