"""Bounded model-owned metric jobs using the existing Agent task journal."""

from __future__ import annotations

import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
from uuid import uuid4

from lab_control.domain.instruments import DeviceError, utc_now
from lab_control.domain.metrics import CapturePlan, MetricInput, MetricValue
from lab_control.storage.metric_results import MetricRecord, MetricStore, digest
from lab_control.storage.processing import check_cancel, iter_source_frames


class MetricTasks:
    def __init__(self, service):
        self.service = service
        self.runtime = service.runtime
        self.store = MetricStore(service.repository)
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="metric-processing")
        self.capacity = 9  # One running job and at most eight waiting jobs.
        self.pending: dict[str, dict] = {}

    def profiles(self, client, grant, device):
        from lab_control.instruments.registry import DEVICE_DEFINITIONS

        self.service._check(client, grant, device, "view")
        definition = DEVICE_DEFINITIONS.get(self.service.identity(device)["adapter"])
        return definition.metric_profiles if definition else ()

    def capabilities(self, client, grant, device):
        profiles = self.profiles(client, grant, device)
        return {
            "supported": bool(profiles),
            "profiles": [
                {
                    "id": p.id,
                    "version": p.version,
                    "description": p.description,
                    "parameters": p.parameters.model_json_schema(),
                    "metrics": [asdict(m) for m in p.metrics],
                    "supports_partial": p.supports_partial,
                    "automatic_capture": p.capture is not None,
                    "capture_parameters": p.capture_parameters.model_json_schema()
                    if p.capture_parameters
                    else None,
                }
                for p in profiles
            ],
        }

    def profile(self, client, grant, device, profile_id):
        for profile in self.profiles(client, grant, device):
            if profile.id == profile_id:
                return profile
        raise DeviceError("此型号未声明该指标处理能力", "processing_unsupported")

    def authorize(self, client, grant, record: MetricRecord, action="results", channels=()):
        self.service._check(client, grant, record.device_id, action, channels=tuple(channels))
        expected = self.service.identity(record.device_id)
        actual = record.device.metadata["identity"]
        if (
            not actual.get("serial")
            or expected["adapter"] != record.device.metadata["adapter_id"]
            or any(
                actual.get(k, "").strip().casefold() != expected[k].strip().casefold()
                for k in ("manufacturer", "model", "serial")
            )
        ):
            raise DeviceError("历史记录缺少完整可核验资产身份或与授权不符", "identity_conflict")

    def inspect(self, client, grant, record, cancel):
        self.authorize(client, grant, record)
        if self.runtime._recording_path_active(record.capture_folder):
            raise DeviceError("记录仍在采集或收尾", "recording_busy")
        info = self.store.inspect(record, cancel)
        self.authorize(client, grant, record, channels=info["channels"])
        return info

    def list_recordings(self, client, grant, device_id=""):
        self.service.policy.require_grant(client, grant)
        records, conflicts = self.store.records()
        values = []
        for record in records.values():
            if device_id and record.device_id != device_id:
                continue
            try:
                self.authorize(client, grant, record)
            except DeviceError:
                continue
            value = {
                "recording_id": record.id,
                "device_id": record.device_id,
                "kind": record.device.metadata.get("kind", "unknown"),
                "started_at": record.device.metadata.get("started_utc", ""),
            }
            try:
                if record.id in conflicts:
                    raise DeviceError("记录 ID 对应多个目录", "recording_conflict")
                value.update(self.inspect(client, grant, record, threading.Event()), available=True)
            except DeviceError as exc:
                if exc.code in {"permission_denied", "grant_expired", "identity_conflict"}:
                    continue
                value.update(available=False, code=exc.code)
            except (OSError, ValueError, KeyError, TypeError):
                value.update(available=False, code="recording_unavailable")
            values.append(value)
        return values

    def list_results(self, client, grant, device_id="", recording_id="", profile_id=""):
        self.service.policy.require_grant(client, grant)
        records, conflicts = self.store.records()
        values = []
        for record in records.values():
            if (
                record.id in conflicts
                or (recording_id and record.id != recording_id)
                or (device_id and record.device_id != device_id)
            ):
                continue
            try:
                self.authorize(client, grant, record)
            except DeviceError:
                continue
            for result in self.store.results(record):
                try:
                    self.authorize(client, grant, record, channels=result["channels"])
                except DeviceError:
                    continue
                if not profile_id or result["profile_id"] == profile_id:
                    values.append(
                        {
                            k: result[k]
                            for k in (
                                "id",
                                "recording_id",
                                "device_id",
                                "profile_id",
                                "profile_version",
                                "completed_at",
                                "complete",
                            )
                        }
                    )
        return values

    def get_result(self, client, grant, result_id):
        self.service.policy.require_grant(client, grant)
        record, result = self.store.get(result_id)
        self.authorize(client, grant, record, channels=result["channels"])
        return result

    def start(
        self,
        client,
        grant,
        device,
        request_id,
        profile_id,
        parameters,
        *,
        recording_id="",
        capture_parameters=None,
        allow_partial=False,
        force=False,
    ):
        service = self.service
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise DeviceError("request_id 必须为 1..128 字符", "invalid_parameter")
        scope = service._check(client, grant, device, "process")
        payload = {
            "profile_id": profile_id,
            "parameters": parameters,
            "recording_id": recording_id,
            "capture_parameters": capture_parameters,
            "allow_partial": allow_partial,
            "force": force,
        }
        request_fingerprint = service.ledger.fingerprint([grant, device, "process", payload])
        # Replay lookup precedes changing sources or updated/removed model capabilities.
        with service.lock:
            previous = service.ledger.find_request(client, request_id, request_fingerprint)
            if previous:
                service._check(client, grant, device, "view", channels=tuple(previous.get("channels", [])))
                return previous
        profile = self.profile(client, grant, device, profile_id)
        params = profile.parameters.model_validate(parameters)
        effective = params.model_dump(mode="json")
        if allow_partial and not profile.supports_partial:
            raise DeviceError("此处理项不支持不完整记录", "partial_unsupported")
        record, source_stamp, plan, key = None, None, None, ""
        if capture_parameters is not None:
            if service.identity(device)["serial"].strip().upper() in {"", "0", "N/A", "UNKNOWN"}:
                raise DeviceError("自动流程需要可核验的设备序列号", "identity_conflict")
            if profile.capture is None or profile.capture_parameters is None:
                raise DeviceError("此处理项未声明自动采集", "capture_unsupported")
            plan = profile.capture(profile.capture_parameters.model_validate(capture_parameters))
            plan.validate()
            plan = replace(plan, request=self.capture_request(client, grant, device, plan))
            if device not in self.runtime.sessions.sessions:
                raise DeviceError("请先通过 connect_device 连接设备", "disconnected")
            if plan.mode == "finite" and plan.request.kind not in getattr(
                self.runtime.sessions.sessions[device].adapter, "recorded_run_kinds", ()
            ):
                raise DeviceError("型号未提供有限采集入口", "capture_unsupported")
            channels = list(plan.request.channels)
        else:
            record = self.store.resolve(recording_id)
            if record.device_id != device:
                raise DeviceError("记录不属于所选设备", "identity_conflict")
            self.authorize(client, grant, record)
            if self.runtime._recording_path_active(record.capture_folder):
                raise DeviceError("记录仍在采集或收尾", "recording_busy")
            if not self.store.ready(record) and not allow_partial:
                raise DeviceError("记录不完整；处理项和请求须同时允许", "incomplete_recording")
            source_stamp = self.store.stamp(record)
            # Until the worker verifies the actual frame channels, visibility is
            # restricted to the complete submitting grant's channel scope.
            channels = sorted(scope["channels"])
            key = digest(
                [record.id, source_stamp, profile.id, profile.version, effective, allow_partial, channels]
            )
        service._check(client, grant, device, "process", channels=tuple(channels))
        service._check(client, grant, device, "results", channels=tuple(channels))

        with service.lock:
            previous = service.ledger.find_request(client, request_id, request_fingerprint)
            if previous:
                return previous
            if key and not force:
                for context in self.pending.values():
                    if context.get("metric_key") == key and not context["cancel"].is_set():
                        service.repository.audit(
                            "agent_metric_joined",
                            client_id=client,
                            request_id=request_id,
                            task_id=context["task"]["id"],
                        )
                        service.ledger.alias(client, request_id, request_fingerprint, context["task"]["id"])
                        return service._lookup_task(context["task"]["id"])
            if len(self.pending) >= self.capacity:
                raise DeviceError("指标处理队列已满", "task_busy")
            reservation = service.repository.record_access.acquire(record.capture_folder) if record else None

            def start(ctx):
                ctx.update(
                    cancel=threading.Event(),
                    metric_key=key,
                    reservation=reservation,
                    source_stamp=source_stamp,
                    capture_channels=channels if plan else None,
                )
                ctx["task"].update(
                    channels=channels,
                    phase="queued",
                    recording_id=recording_id,
                    profile_id=profile.id,
                    profile_version=profile.version,
                    task_type="experiment" if plan else "processing",
                )
                self.pending[ctx["task"]["id"]] = ctx
                try:
                    ctx["future"] = self.worker.submit(run, ctx)
                except Exception:
                    self.pending.pop(ctx["task"]["id"], None)
                    if ctx.get("reservation") is not None:
                        service.repository.record_access.release(ctx.pop("reservation"))
                    raise

            def run(ctx):
                try:
                    self.check(ctx)
                    current = record
                    if plan:
                        current = self.capture(ctx, plan)
                    assert current is not None
                    if ctx["reservation"] is None:
                        ctx["reservation"] = service.repository.record_access.acquire(current.capture_folder)
                    return self.compute(ctx, current, profile, params, effective, allow_partial, force)
                finally:
                    if ctx.get("reservation") is not None:
                        service.repository.record_access.release(ctx.pop("reservation"))
                    with service.lock:
                        self.pending.pop(ctx["task"]["id"], None)

            try:
                return service._task(client, grant, device, "process", request_id, payload, start)
            except Exception:
                if reservation is not None:
                    service.repository.record_access.release(reservation)
                raise

    def check(self, ctx):
        check_cancel(ctx["cancel"])
        task = ctx["task"]
        self.service._check(
            task["client_id"],
            task["grant_id"],
            task["device_id"],
            "process",
            channels=tuple(task["channels"]),
        )
        self.service._check(
            task["client_id"],
            task["grant_id"],
            task["device_id"],
            "results",
            channels=tuple(task["channels"]),
        )

    def update(self, ctx, **values):
        with self.service.lock:
            ctx["task"].update(values)
            if not self.service._persist(ctx):
                raise DeviceError("任务状态无法持久保存，未继续计算", "task_persistence_failed")

    def capture_request(self, client, grant, device, plan: CapturePlan):
        request = {
            k: getattr(plan.request, k)
            for k in (
                "kind",
                "channels",
                "points",
                "quantity",
                "initiate",
                "waveform_extent",
                "duration",
                "sample_rate",
            )
        }
        return self.service._request(
            client, grant, device, "acquire" if plan.mode == "single" else "record", request
        )

    def capture(self, ctx, plan):
        service, task = self.service, ctx["task"]
        device = task["device_id"]
        self.update(ctx, phase="acquiring")

        def guard(session):
            self.check(ctx)
            self.capture_request(task["client_id"], task["grant_id"], device, plan)

        if plan.mode == "single":
            session = self.runtime.sessions.sessions[device]
            context = service.repository.new_save_context(
                "general",
                "single_frame",
                device_id=device,
                identity=session.identity,
                adapter_id=session.config.adapter,
            )
            batch = self.runtime.sessions.submit(device, "acquire", plan.request, before_io=guard).result()
            self.runtime.monitoring._validate_saved_batch(batch)
            self.update(ctx, phase="saving")
            folder = service.repository.export(batch, context=context)
        else:
            with service.lock:
                self.check(ctx)
                future = self.runtime.sessions.start_acquisition(
                    device, plan.request, count=plan.count, interval=plan.interval, before_io=guard
                )
                recorder = self.runtime.sessions.sessions[device].direct_recording
                ctx["direct_recording"] = recorder
                ctx["folder"] = str(recorder.folder)
            while not future.done():
                if recorder.binary_done and task.get("phase") != "saving":
                    self.update(ctx, phase="saving")
                time.sleep(0.05)
            future.result()
            self.check(ctx)
            if plan.mode == "count" and recorder.committed != plan.count:
                raise DeviceError("采集提前结束，未达到指定帧数", "incomplete_recording")
            folder = recorder.folder
        ctx["folder"] = str(folder)
        records, conflicts = self.store.records()
        current = next((r for r in records.values() if r.device.folder == folder), None)
        if current is None or current.id in conflicts:
            raise DeviceError("采集记录无法唯一登记", "recording_unavailable")
        self.update(ctx, recording_id=current.id)
        self.check(ctx)
        return current

    def compute(self, ctx, record, profile, params, effective, allow_partial, force):
        task = ctx["task"]
        self.update(ctx, phase="validating")
        if ctx.get("source_stamp") is not None and self.store.stamp(record) != ctx["source_stamp"]:
            raise DeviceError("排队期间原始记录变化，未开始计算", "source_changed")
        info = self.inspect(task["client_id"], task["grant_id"], record, ctx["cancel"])
        if ctx.get("capture_channels") is not None and set(info["channels"]) != set(ctx["capture_channels"]):
            raise DeviceError("原始记录通道发生变化", "source_changed")
        self.update(ctx, channels=info["channels"])
        if not info["complete"] and not allow_partial:
            raise DeviceError("记录不完整，未开始处理", "incomplete_recording")
        key = digest([record.id, info["fingerprint"], profile.id, profile.version, effective, allow_partial])
        self.check(ctx)
        if not force:
            for saved in self.store.results(record):
                if saved["cache_key"] == key and saved["complete"]:
                    self.update(ctx, phase="reused", result_id=saved["id"])
                    return {
                        "result_id": saved["id"],
                        "recording_id": record.id,
                        "reused": True,
                        "complete": saved["complete"],
                    }
        self.update(ctx, phase="processing", progress={"done": 0, "total": info["frames"]})
        last_progress = 0.0

        def progress(done, total):
            nonlocal last_progress
            self.check(ctx)
            if type(done) is not int or type(total) is not int or not 0 <= done <= total:
                raise ValueError("Invalid processing progress")
            if done == total or time.monotonic() - last_progress >= 0.2:
                self.update(ctx, progress={"done": done, "total": total})
                last_progress = time.monotonic()

        def frames():
            for frame in iter_source_frames(record.device):
                self.check(ctx)
                yield frame

        source = MetricInput(
            record.id,
            deepcopy(record.device.metadata),
            info["complete"],
            frames,
            lambda: self.check(ctx),
            progress,
        )
        measured = profile.process(source, params)
        if not isinstance(measured, dict) or set(measured) != {m.name for m in profile.metrics}:
            raise ValueError("处理器返回的指标与声明不一致")
        metrics = {}
        for definition in profile.metrics:
            item = measured[definition.name]
            if not isinstance(item, MetricValue) or item.quality not in {"good", "simulated", "unavailable"}:
                raise ValueError("Invalid metric value/quality")
            if item.value is None:
                if item.quality != "unavailable":
                    raise ValueError("Missing metrics must be marked unavailable")
            elif (
                type(item.value) not in (float, int)
                or not math.isfinite(item.value)
                or item.quality == "unavailable"
            ):
                raise ValueError("Metrics must contain finite numbers")
            metrics[definition.name] = {
                "value": item.value,
                "unit": definition.unit,
                "quality": "simulated"
                if info["quality"] == "simulated" and item.value is not None
                else item.quality,
            }
        self.check(ctx)
        if self.store.fingerprint(record, ctx["cancel"]) != info["fingerprint"]:
            raise DeviceError("计算期间原始数据变化，结果未发布", "source_changed")
        result = {
            "schema_version": 1,
            "id": uuid4().hex,
            "recording_id": record.id,
            "device_id": record.device_id,
            "identity": record.device.metadata["identity"],
            "channels": info["channels"],
            "source_fingerprint": info["fingerprint"],
            "profile_id": profile.id,
            "profile_version": profile.version,
            "parameters": effective,
            "cache_key": key,
            "completed_at": utc_now(),
            "complete": info["complete"],
            "quality": info["quality"],
            "metrics": metrics,
        }
        self.update(ctx, phase="publishing")
        with self.service.lock:
            self.check(ctx)
            self.service.repository.audit(
                "agent_metric_publishing", task_id=task["id"], result_id=result["id"]
            )
            self.store.publish(record, result)
            try:
                self.update(ctx, phase="completed", result_id=result["id"])
            except Exception:
                task.pop("result_id", None)
                task["phase"] = "failed"
                self.store.invalidate(record, result["id"])
                raise
        return {
            "result_id": result["id"],
            "recording_id": record.id,
            "reused": False,
            "complete": info["complete"],
        }

    def close(self):
        self.worker.shutdown(wait=True)
