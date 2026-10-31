"""A worker-local read budget for continuous acquisition, never a retry loop."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from typing import Any, Iterator

_minimum: ContextVar[float] = ContextVar("acquisition_read_wait", default=0.0)
CONTINUOUS_READ_WAIT = 10.0


@contextmanager
def acquisition_read_wait(enabled: bool = True, *, transport: Any = None) -> Iterator[None]:
    token = _minimum.set(CONTINUOUS_READ_WAIT if enabled else 0.0)
    try:
        scope = getattr(transport, "acquisition_timeout", None)
        with scope() if enabled and callable(scope) else nullcontext():
            yield
    finally:
        _minimum.reset(token)


def read_timeout(seconds: float) -> float:
    """Models with explicit sample/SDK deadlines retain any larger budget."""
    return max(seconds, _minimum.get())


@contextmanager
def temporary_timeout(transport: Any, attribute: str, units: float = 1.0) -> Iterator[None]:
    """Change before requests, never reconfigure midway through an incoming frame.

    pySerial's timeout setter also reconfigures the Windows port. VISA uses
    milliseconds; serial and sockets seconds. Preserve longer model deadlines.
    """
    handle = getattr(transport, attribute, None)
    socket_style = attribute == "sock"
    if not _minimum.get() or handle is None or (not socket_style and not hasattr(handle, "timeout")):
        yield
        return
    old = handle.gettimeout() if socket_style else handle.timeout
    target = None if old is None else max(old, _minimum.get() * units)
    if target == old:
        yield
        return

    def set_value(value):
        if socket_style:
            handle.settimeout(value)
        else:
            handle.timeout = value

    set_value(target)
    try:
        yield
    except BaseException:
        if getattr(transport, attribute, None) is handle:
            try:
                set_value(old)
            except Exception:
                pass  # Preserve the original failure of a disconnected handle.
        raise
    else:
        if getattr(transport, attribute, None) is handle:
            set_value(old)
