"""Single, bounded consumer of the vendor report queue; no device commands."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from typing import Any

from lab_control.domain.instruments import DeviceError

from .protocol import FrequencyReport, PhaseReport, parse_frequency_report, parse_phase_report

DecodedReport = FrequencyReport | PhaseReport


class ReportReader:
    def __init__(self, library: Any, source_id: int) -> None:
        self.library, self.source_id = library, source_id
        self.pending: deque[tuple[DecodedReport, str]] = deque()
        self.logs: deque[str] = deque(maxlen=64)
        self.limit = 64
        self.channel_count = 0
        self.interval_ms = 0
        self.revision = 0
        self.signature: tuple[int, int, str] | None = None
        self.clock = time.monotonic
        self.sleep = time.sleep
        self.on_report: Callable[[Any], None] | None = None

    def poll(self) -> bool:
        report = self.library.get_kk_report(self.source_id)
        kind = int(getattr(report.get_report_type(), "value", report.get_report_type()))
        if kind == 0:
            return False
        if self.on_report:
            self.on_report(report)
        log = str(getattr(report, "to_log_str", lambda: "")() or "")
        self.logs.append(log)
        # Decode immediately: the official wrapper reuses internal lists.
        decoded: DecodedReport | None = parse_frequency_report(report)
        if decoded is None:
            decoded = parse_phase_report(report)
        if decoded is not None:
            if len(self.pending) >= self.limit:
                raise DeviceError("K+K 测量报告缓冲已满；请停止并重新连接", "buffer_overflow")
            count = len(decoded.parts) if isinstance(decoded, PhaseReport) else len(decoded.values)
            signature = (decoded.header, count, type(decoded).__name__)
            if signature != self.signature:
                self.signature = signature
                self.channel_count, self.interval_ms = count, decoded.interval_ms
                self.revision += 1
            self.pending.append((decoded, log))
        return True

    def read_logs(self, limit: int = 8) -> tuple[str, ...]:
        for _ in range(limit):
            if not self.poll():
                break
        return tuple(self.logs)

    def wait(self, deadline: float) -> None:
        busy = 0
        while not self.pending:
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise DeviceError("K+K did not return a frequency report；等待有效测量报告超时", "timeout")
            if not self.poll():
                self.sleep(min(0.05, remaining))
            else:
                busy += 1
                if busy % 64 == 0:
                    self.sleep(min(0.001, remaining))

    def wait_seconds(self) -> float:
        if not self.interval_ms:
            return 30.0
        return min(45.0, max(10.0, 2 * self.interval_ms / 1000 + 5))

    def next(self, deadline: float) -> tuple[DecodedReport, str]:
        self.wait(deadline)
        return self.pending.popleft()
