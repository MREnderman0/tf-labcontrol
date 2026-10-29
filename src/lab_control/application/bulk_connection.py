"""One bounded, cancellable connection batch using the normal session boundary."""

from __future__ import annotations

import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from lab_control.application.config import config_from_record
from lab_control.discovery.presence import LIVE_CONNECTIONS, is_current
from lab_control.domain.instruments import DeviceError, utc_now
from lab_control.instruments.registry import describe

if TYPE_CHECKING:
    from lab_control.application.runtime import ApplicationRuntime


def device_label(record: dict[str, Any]) -> str:
    identity = record.get("identity", {})
    config = record.get("config", {})
    # Model-only protocols may not be able to read an instrument serial.  Keep a
    # configured asset serial visible for selection; fall back to the concrete
    # resource so two identical units never collapse into one indistinguishable
    # label.
    serial = identity.get("serial") or config.get("expected_serial")
    stable_hint = config.get("usb_instance_id", "")
    if stable_hint and "\\" in stable_hint:
        stable_hint = stable_hint.rsplit("\\", 1)[-1]
    resource_hint = "" if serial or stable_hint else config.get("resource")
    parts = (record.get("displayName"), identity.get("model"), serial or stable_hint or resource_hint)
    return " / ".join(dict.fromkeys(part for part in parts if part)) or record["config"]["resource"]


class BulkConnectionService:
    max_inflight = 4

    def __init__(self, runtime: ApplicationRuntime) -> None:
        self.runtime = runtime
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bulk-connection")
        self.cancelled = threading.Event()
        self.future: Future[dict[str, Any]] | None = None
        self._progress: dict[str, Any] = {"state": "idle", "total": 0, "completed": 0, "items": []}

    def snapshot(self) -> dict[str, Any]:
        with self.runtime.discovery.lock:
            return deepcopy(self._progress)

    def start(self) -> Future[dict[str, Any]]:
        runtime = self.runtime
        with runtime.discovery.lock:
            if runtime.discovery.closing or runtime.closed:
                raise DeviceError("程序正在退出", "runtime_closing")
            if self.future and not self.future.done():
                raise DeviceError("正在批量连接，请等待完成或停止连接", "connection_batch_busy")
            if runtime.discovery.future and not runtime.discovery.future.done():
                raise DeviceError("正在扫描并核验设备，请等待扫描完成后连接", "scan_busy")
            states = {s["stableDeviceId"]: s for s in runtime.sessions.snapshots()}
            records = tuple(r for r in runtime.inventory.all() if is_current(r, states.get(r["id"])))
            self.cancelled.clear()
            self._progress = {
                "state": "running",
                "startedAt": utc_now(),
                "total": len(records),
                "completed": 0,
                "current": "",
                "active": [],
                "queued": len(records),
                "parallel_limit": self.max_inflight,
                "items": [],
            }
            runtime.discovery.connection_batch_active = True
            try:
                self.future = self.worker.submit(self._run, records)
            except Exception:
                runtime.discovery.connection_batch_active = False
                self._progress.update(state="error", current="", finishedAt=utc_now())
                raise
            return self.future

    def cancel(self) -> None:
        with self.runtime.discovery.lock:
            if self.runtime.discovery.connection_batch_active and self._progress["state"] in {
                "running",
                "stopping",
            }:
                self.cancelled.set()
                self._progress["state"] = "stopping"

    def _start_one(self, record: dict[str, Any]) -> tuple[Future[Any] | None, str, str]:
        runtime = self.runtime
        device_id = record["id"]
        states = {s["stableDeviceId"]: s for s in runtime.sessions.snapshots()}
        state = states.get(device_id)
        if state:
            if state["connectionState"] in LIVE_CONNECTIONS:
                return None, "already_connected", state.get("lastError", "") or "已有连接，保持当前会话"
            return None, "skipped", "已有连接任务或异常会话，请在设备池中处理"
        current = next((r for r in runtime.inventory.all() if r["id"] == device_id), None)
        if current is None or not is_current(current):
            return None, "skipped", "设备已不在当前发现范围"
        if current.get("identityConflict") or current["discoveryState"] == "conflict":
            return None, "skipped", "设备身份冲突，请先核对"
        if not current.get("adapterId"):
            return None, "skipped", "未知型号，尚未选择准确适配器"
        try:
            describe(config_from_record(current))
        except DeviceError as exc:
            return None, "skipped", str(exc)
        return runtime._connect(device_id), "connected", "连接成功"

    def _connect_one(self, record: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        runtime = self.runtime
        try:
            future, status, reason = self._start_one(record)
            if future is not None:
                future.result()
                current = next(r for r in runtime.inventory.all() if r["id"] == record["id"])
                item["label"] = device_label(current)
                if current.get("identityConflict"):
                    raise DeviceError("连接后发现身份冲突，请在设备池中核对", "identity_conflict")
                state = next(s for s in runtime.sessions.snapshots() if s["stableDeviceId"] == record["id"])
                if state["connectionState"] == "degraded":
                    raise DeviceError(state["lastError"] or "连接异常，请核对设备状态", "connection_degraded")
                if state.get("lastError"):
                    reason = "连接已建立；" + state["lastError"]
            item.update(status=status, reason=reason)
        except Exception as exc:
            item.update(status="failed", reason=str(exc), error_code=getattr(exc, "code", ""))
        return item

    def _run(self, records: tuple[dict[str, Any], ...]) -> dict[str, Any]:
        runtime = self.runtime
        pending: dict[Future[dict[str, Any]], tuple[int, dict[str, Any]]] = {}
        completed: dict[int, dict[str, Any]] = {}
        next_index = 0
        failed = False
        workers = ThreadPoolExecutor(max_workers=self.max_inflight, thread_name_prefix="connection-admission")

        def progress() -> None:
            active = [dict(entry[1]) for entry in pending.values()]
            with runtime.discovery.lock:
                self._progress.update(
                    active=active,
                    current="、".join(item["label"] for item in active),
                    queued=len(records) - next_index,
                    completed=len(completed),
                    items=[completed[index] for index in sorted(completed)],
                )

        def finish(index: int, item: dict[str, Any]) -> None:
            item["finishedAt"] = utc_now()
            completed[index] = item
            progress()
            runtime.repository.audit("bulk_connection_item", **item)

        try:
            while next_index < len(records) or pending:
                # Reserve bounded background admissions under the gate, without
                # running qualification or waiting for a VISA driver under the
                # discovery lock used by GUI refresh and cancellation. Each
                # admission still opens through the normal per-device worker.
                while next_index < len(records) and len(pending) < self.max_inflight:
                    index = next_index
                    record = records[index]
                    next_index += 1
                    item = {"device_id": record["id"], "label": device_label(record)}
                    try:
                        with runtime.discovery.lock:
                            if runtime.discovery.closing:
                                self.cancelled.set()
                            if self.cancelled.is_set():
                                future = None
                            else:
                                item["startedAt"] = utc_now()
                                future = workers.submit(self._connect_one, record, dict(item))
                        if future is not None:
                            pending[future] = (index, item)
                            progress()
                            continue
                        item.update(status="skipped", reason="已停止，未尝试连接")
                    except Exception as exc:
                        item.update(status="failed", reason=str(exc), error_code=getattr(exc, "code", ""))
                    finish(index, item)

                if not pending:
                    continue
                # Completion order is independent of submission order. A slow
                # device keeps just one slot; each free slot admits the next.
                done, _ = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                for future in sorted(done, key=lambda task: pending[task][0]):
                    index, _ = pending.pop(future)
                    finish(index, future.result())

                # Cancellation skips queued devices immediately, but admitted
                # session opens still drain under the batch exclusion gate.
                if self.cancelled.is_set():
                    while next_index < len(records):
                        index, record = next_index, records[next_index]
                        next_index += 1
                        finish(
                            index,
                            {
                                "device_id": record["id"],
                                "label": device_label(record),
                                "status": "skipped",
                                "reason": "已停止，未尝试连接",
                            },
                        )
            with runtime.discovery.lock:
                self._progress.update(
                    state="cancelled" if self.cancelled.is_set() else "completed",
                    current="",
                    active=[],
                    queued=0,
                    finishedAt=utc_now(),
                )
                result = deepcopy(self._progress)
            runtime.repository.audit("bulk_connection_finished", result=result)
            return result
        except Exception:
            failed = True
            with runtime.discovery.lock:
                self._progress.update(state="stopping")
            raise
        finally:
            # An unexpected coordinator/audit failure must not release admission
            # while an accepted open is still touching a device or saving identity.
            workers.shutdown(wait=True)
            with runtime.discovery.lock:
                self._progress.update(current="", active=[])
                if failed:
                    self._progress.update(state="error", finishedAt=utc_now())
                runtime.discovery.connection_batch_active = False

    def close(self) -> None:
        self.cancel()
        self.worker.shutdown(wait=True)
