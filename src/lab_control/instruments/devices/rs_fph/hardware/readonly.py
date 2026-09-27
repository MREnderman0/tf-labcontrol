"""Import-safe explicit read-only entry; the runner defaults to no setting writes."""

from .validate_control import main

if __name__ == "__main__":
    raise SystemExit(main())
