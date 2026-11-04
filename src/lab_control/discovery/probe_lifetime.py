"""Preserve uncertain probe handles until their owner can confirm closure."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from lab_control.domain.instruments import ResourceCleanupError as ProbeCleanupError


@contextmanager
def probe_handle(resource: Any) -> Iterator[Any]:
    primary = None
    try:
        yield resource
    except Exception as exc:
        primary = exc
        raise
    finally:
        try:
            resource.close()
        except Exception as exc:
            raise ProbeCleanupError(resource, exc, primary) from exc
