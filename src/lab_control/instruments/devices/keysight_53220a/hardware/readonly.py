"""Stable, query-only hardware entry used by package and installed-wheel checks."""

from .verify import verify_readonly as verify

__all__ = ["verify"]
