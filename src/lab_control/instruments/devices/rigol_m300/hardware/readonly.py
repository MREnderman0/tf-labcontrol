"""Import-safe M300 hardware entry; no access until ``main`` is called."""

from .verify import main as verify_main
from .verify import verify_readonly as verify

__all__ = ["main", "verify"]


def main(argv=None):
    return verify_main(argv, readonly=True)


if __name__ == "__main__":
    raise SystemExit(main())
