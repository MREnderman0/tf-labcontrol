"""Read-only verification uses the same runner with setting writes disabled."""

from .validate_control import main

if __name__ == "__main__":
    raise SystemExit(main())
