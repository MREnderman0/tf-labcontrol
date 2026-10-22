"""Shared live monitoring, scheduled through the existing per-device session workers."""

from __future__ import annotations

import time
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from lab_control.domain.instruments import (
    AcquisitionRequest,
    MeasurementBatch,
)
from lab_control.domain.workspace import GENERAL
from lab_control.storage.paths import SaveContext
from lab_control.storage.recording import Recording


@dataclass(frozen=True)
class MonitorFrame:
    sequence: int
    configuration_version: int
    request: AcquisitionRequest
    batch: MeasurementBatch
    received_at: float = field(default_factory=time.perf_counter)
    session_id: str = ""


@dataclass
class CaptureSave:
    read: Future[Any]
    result: Future[Any]
    mode: str
    write: Future[Any] | None = None
    display_request: AcquisitionRequest | None = None
    session_id: str = ""
    save_context: SaveContext | None = None


@dataclass
class MonitorTask:
    requests: tuple[AcquisitionRequest, ...]
    mode: str
    interval: float
    record_interval: float | None = 0.05
    running: bool = True
    recording: bool = False
    pending: Future[Any] | None = None
    pending_request: AcquisitionRequest | None = None
    request_revision: int = 0
    pending_revision: int = 0
    write_pending: Future[Any] | None = None
    next_at: float = 0
    index: int = 0
    sequence: int = 0
    frames: dict[tuple[str, str], MonitorFrame] = field(default_factory=dict)
    # Bounded replay window for scalar workspaces. The latest-frame map above
    # remains the fast path; this lets hidden pages catch up without affecting
    # acquisition or recording.
    frame_history: dict[tuple[str, str], deque[MonitorFrame]] = field(default_factory=dict)
    error: str = ""
    last_path: str = ""
    stop_future: Future[Any] | None = None
    record_session: str = ""
    acquisition_context_id: str = ""
    monitoring_context_id: str = field(default_factory=lambda: uuid4().hex)
    once: bool = False
    pending_record_session: str = ""
    pending_record_mode: str = GENERAL
    last_started_at: float = 0
    last_record_at: float = 0
    next_record_at: float = 0
    last_frame_at: float = 0
    frame_interval: float | None = None
    acquisition_seconds: float | None = None
    configuration_changes: int = 0
    configuration_wait_deadline: float = 0
    configuration_wait_expired: bool = False
    notice: str = ""
    cadence: str = "interval"
    csv_interval: float = 5
    recorder: Recording | None = None
    recording_notice: str = ""
    display_seconds: float | None = None
    scheduler_seconds: float = 0
    timings: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=120))
    timing_report_at: float = 0
