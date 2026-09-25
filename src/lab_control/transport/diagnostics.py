"""Bounded, thread-local I/O accounting. Never retain instrument payloads."""

from __future__ import annotations

import os
import re
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from typing import Any


@dataclass
class IOCapture:
    queries: int = 0
    writes: int = 0
    reads: int = 0
    sent_bytes: int = 0
    received_bytes: int = 0
    write_seconds: float = 0
    read_seconds: float = 0
    parse_seconds: float = 0
    details: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {key: value for key, value in vars(self).items() if key != "details"}


_current: ContextVar[IOCapture | None] = ContextVar("instrument_io_capture", default=None)


def synchronized(function: Callable) -> Callable:
    @wraps(function)
    def invoke(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self.lock:
            return function(self, *args, **kwargs)

    return invoke


@contextmanager
def capture_io() -> Iterator[IOCapture]:
    capture = IOCapture()
    token = _current.set(capture)
    try:
        yield capture
    finally:
        _current.reset(token)


@contextmanager
def parsing() -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        capture = _current.get()
        if capture is not None:
            capture.parse_seconds += time.perf_counter() - started


def measured(stage: str) -> Callable:
    """Count query once, and its nested write/read separately; preserve exceptions."""

    def decorate(function: Callable) -> Callable:
        @wraps(function)
        def invoke(*args: Any, **kwargs: Any) -> Any:
            capture = _current.get()
            if capture is None:
                return function(*args, **kwargs)
            started = time.perf_counter()
            result: Any = None
            error = ""
            if stage == "query":
                capture.queries += 1
            elif stage == "write":
                capture.writes += 1
                payload = args[1] if len(args) > 1 else kwargs.get("payload", b"")
                capture.sent_bytes += len(payload.encode("ascii") if isinstance(payload, str) else payload)
            elif stage == "read":
                capture.reads += 1
            try:
                result = function(*args, **kwargs)
                return result
            except Exception as exc:
                error = str(getattr(exc, "code", type(exc).__name__))
                raise
            finally:
                elapsed = time.perf_counter() - started
                if stage == "write":
                    capture.write_seconds += elapsed
                elif stage == "read":
                    capture.read_seconds += elapsed
                    capture.received_bytes += len(result) if isinstance(result, bytes) else 0
                if stage == "query" and os.environ.get("LAB_CONTROL_IO_DIAGNOSTICS") == "1":
                    payload = args[1] if len(args) > 1 else kwargs.get("payload", b"")
                    match = (
                        re.match(r"^[A-Za-z:*][A-Za-z0-9:*?]*", payload) if isinstance(payload, str) else None
                    )
                    if len(capture.details) < 256:
                        capture.details.append(
                            {
                                "command": match.group() if match else "binary_or_unclassified",
                                "seconds": elapsed,
                                "bytes": len(result) if isinstance(result, bytes) else 0,
                                "error": error,
                            }
                        )

        return invoke

    return decorate


class AcquisitionStatistics:
    """Per-session rolling statistics, with no waveform or identity storage."""

    def __init__(self) -> None:
        self.frames: deque[dict[str, Any]] = deque(maxlen=120)
        self.last_report = time.monotonic()

    def add(self, performance: dict[str, Any], validation: str) -> dict[str, Any] | None:
        self.frames.append({**performance, "validation": validation})
        now = time.monotonic()
        if now - self.last_report < 5:
            return None
        self.last_report = now
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        frames = list(self.frames)
        full = [f["acquisition_seconds"] for f in frames if f["validation"] == "full"]
        return {
            "frames": len(frames),
            "latest": frames[-1] if frames else {},
            "mean_acquisition_seconds": sum(f["acquisition_seconds"] for f in frames) / len(frames)
            if frames
            else 0,
            "full_acquisition_seconds": full[-1] if full else None,
        }
