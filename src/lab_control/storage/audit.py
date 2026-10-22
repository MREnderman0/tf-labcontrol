"""Failure-isolated diagnostics; required pre-I/O audit writes remain explicit."""

from collections import deque
from threading import Lock
from typing import Any, Callable

from lab_control.domain.instruments import utc_now


class AuditSink:
    """Keep bounded evidence if diagnostics cannot be persisted, without recursion."""

    def __init__(self, write: Callable[..., None]) -> None:
        self._write = write
        self._lock = Lock()
        self._failures: deque[dict[str, str]] = deque(maxlen=32)

    def try_record(self, event: str, **details: Any) -> bool:
        try:
            self._write(event, **details)
            return True
        except Exception as exc:
            with self._lock:
                self._failures.append({"timestamp": utc_now(), "event": event, "error": str(exc)})
            return False

    def failures(self) -> tuple[dict[str, str], ...]:
        with self._lock:
            return tuple(dict(item) for item in self._failures)
