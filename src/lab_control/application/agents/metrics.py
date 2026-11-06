"""Bounded model-owned metric jobs using the existing Agent task journal."""

from __future__ import annotations

import hashlib
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
from uuid import uuid4

from lab_control.application.agents.command_processing import (
    LOCAL_COMMAND_PROFILE,
    LocalCommand,
    command_capability,
)
from lab_control.application.agents.policy import COMMAND_CLIENT
from lab_control.domain.instruments import DeviceError, utc_now
from lab_control.domain.metrics import (
    MAX_EXPERIMENT_RECORDINGS,
    CapturePlan,
    MetricInput,
    MetricProfile,
    MetricValue,
)
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

    def profiles(self, client, grant, device) -> tuple[MetricProfile, ...]:
        from lab_control.instruments.registry import DEVICE_DEFINITIONS

        self.service._check(client, grant, device, "view")
        definition = DEVICE_DEFINITIONS.get(self.service.identity(device)["adapter"])
        return definition.metric_profiles if definition else ()

    def capabilities(self, client, grant, device, *, local_commands=False):
        profiles: tuple[MetricProfile, ...]
        if local_commands and not grant:
            self.service._check(COMMAND_CLIENT, "", device, "view")
            profiles = ()
        else:
            profiles = self.profiles(client, grant, device)
        advertised = [
            {
                "id": p.id,
                "version": p.version,
                "description": p.description,
                "parameters": p.parameters.model_json_schema(),
                "metrics": [asdict(m) for m in p.metrics],
                **({"per_channel_metrics": True} if p.per_channel_metrics else {}),
                "supports_multiple_recordings": p.supports_multiple_recordings,
                "supports_partial": p.supports_partial,
                "automatic_capture": p.capture is not None,
                "capture_parameters": p.capture_parameters.model_json_schema()
                if p.capture_parameters
                else None,
            }
            for p in profiles
        ]
        if local_commands:
            advertised.append(command_capability())
        return {"supported": bool(advertised), "profiles": advertised}

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
                                "recording_ids",
                                "device_id",
                                "profile_id",
                                "profile_version",
                                "completed_at",
                                "complete",
                            )
                            if k in result
                        }
                    )
        return values

    def get_result(self, client, grant, result_id, *, local_commands=False):
        command_access = local_commands and not grant
        if not command_access:
            self.service.policy.require_grant(client, grant)
        record, result = self.store.get(result_id)
        if command_access:
            if result["profile_id"] != LOCAL_COMMAND_PROFILE:
                raise DeviceError("普通指标结果需要 grant_id", "grant_expired")
            client = COMMAND_CLIENT
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
        recording_ids=None,
        capture_parameters=None,
        allow_partial=False,
        force=False,
        command=None,
    ):
        service = self.service
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise DeviceError("request_id 必须为 1..128 字符", "invalid_parameter")
        if recording_ids is not None and (
            not isinstance(recording_ids, list)
            or not 1 <= len(recording_ids) <= MAX_EXPERIMENT_RECORDINGS
            or any(not isinstance(item, str) for item in recording_ids)
            or len(set(recording_ids)) != len(recording_ids)
            or recording_id
            or capture_parameters is not None
        ):
            raise DeviceError("recording_ids 必须是非空、无重复的记录 ID 列表", "invalid_parameter")
        scope = service._check(client, grant, device, "process")
        force = force or command is not None
        payload = {
            "profile_id": profile_id,
            "parameters": parameters,
            "recording_id": recording_id,
            "capture_parameters": capture_parameters,
            "allow_partial": allow_partial,
            "force": force,
        }
        if recording_ids is not None:
            payload["recording_ids"] = recording_ids
        if command is not None:
            payload["command_sha256"] = hashlib.sha256(command.encode()).hexdigest()
        request_fingerprint = service.ledger.fingerprint([grant, device, "process", payload])
        # Replay lookup precedes changing sources or updated/removed model capabilities.
        with service.lock:
            previous = service.ledger.find_request(client, request_id, request_fingerprint)
            if previous:
                service._check_task_view(client, grant, previous)
                return previous
        if command is None:
            profile = self.profile(client, grant, device, profile_id)
            params = profile.parameters.model_validate(parameters)
            effective = params.model_dump(mode="json")
        else:
            profile = LocalCommand.parse(command, service.repository.root)
            params = effective = parameters
        if allow_partial and not profile.supports_partial:
            raise DeviceError("此处理项不支持不完整记录", "partial_unsupported")
        records: tuple[MetricRecord, ...] = ()
        source_stamps, plan, key = None, None, ""
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
            ids = tuple(recording_ids) if recording_ids is not None else (recording_id,)
            if len(ids) > 1 and not profile.supports_multiple_recordings:
                raise DeviceError("此处理项每次只接收一份记录", "invalid_parameter")
            records = self.store.resolve_many(ids)
            for record in records:
                if record.device_id != device:
                    raise DeviceError("记录不属于所选设备", "identity_conflict")
                self.authorize(client, grant, record)
                if len(records) > 1 and record.device.metadata.get("kind") != "single_frame":
                    raise DeviceError("多条输入记录必须是同一设备的单帧记录", "invalid_parameter")
                if self.runtime._recording_path_active(record.capture_folder):
                    raise DeviceError("记录仍在采集或收尾", "recording_busy")
                if not self.store.ready(record) and not allow_partial:
                    raise DeviceError("记录不完整；处理项和请求须同时允许", "incomplete_recording")
            source_stamps = tuple(self.store.stamp(record) for record in records)
            # Until the worker verifies the actual frame channels, visibility is
            # restricted to the complete submitting grant's channel scope.
            channels = sorted(scope["channels"])
            key = digest(
                [ids, source_stamps, profile.id, profile.version, effective, allow_partial, channels]
            )
        service._check(client, grant, device, "process", channels=tuple(channels))
        service._check(client, grant, device, "results", channels=tuple(channels))

        with service.lock:
            previous = service.ledger.find_request(client, request_id, request_fingerprint)
            if previous:
                service._check_task_view(client, grant, previous)
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
            access = service.repository.record_access
            reservations = list(access.acquire_many(record.capture_folder for record in records))

            def release_reservations():
                while reservations:
                    access.release(reservations.pop())

            def start(ctx):
                ctx.update(
                    cancel=threading.Event(),
                    metric_key=key,
                    source_stamps=source_stamps,
                    capture_channels=channels if plan else None,
                )
                ctx["task"].update(
                    channels=channels,
                    phase="queued",
                    recording_id=records[0].id if records else "",
                    profile_id=profile.id,
                    profile_version=profile.version,
                    task_type="experiment" if plan or recording_ids is not None else "processing",
                )
                if len(records) > 1:
                    ctx["task"]["recording_ids"] = [record.id for record in records]
                if not channels:
                    ctx["task"]["require_full_scope"] = True
                self.pending[ctx["task"]["id"]] = ctx
                try:
                    ctx["future"] = self.worker.submit(run, ctx)
                except Exception:
                    self.pending.pop(ctx["task"]["id"], None)
                    release_reservations()
                    raise

            def run(ctx):
                try:
                    self.check(ctx)
                    current = records
                    if plan:
                        record = self.capture(ctx, plan)
                        current = (record,)
                        reservations.append(access.acquire(record.capture_folder))
                    return self.compute(ctx, current, profile, params, effective, allow_partial, force)
                finally:
                    with service.lock:
                        try:
                            release_reservations()
                        finally:
                            self.pending.pop(ctx["task"]["id"], None)

            try:
                return service._task(client, grant, device, "process", request_id, payload, start)
            except Exception:
                release_reservations()
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

    def compute(self, ctx, records, profile, params, effective, allow_partial, force):
        task = ctx["task"]
        anchor = records[0]
        recording_ids = tuple(record.id for record in records)
        source_reference = {"recording_id": anchor.id}
        if len(records) > 1:
            source_reference["recording_ids"] = list(recording_ids)
        self.update(ctx, phase="validating")
        infos = []
        stamps = ctx.get("source_stamps")
        for index, record in enumerate(records):
            check_cancel(ctx["cancel"])
            if stamps is not None and self.store.stamp(record) != stamps[index]:
                raise DeviceError("排队期间原始记录变化，未开始计算", "source_changed")
            infos.append(self.inspect(task["client_id"], task["grant_id"], record, ctx["cancel"]))
        if len(records) > 1 and any(info["frames"] != 1 for info in infos):
            raise DeviceError("多条输入记录必须各含一帧", "recording_unavailable")
        channels = sorted({channel for info in infos for channel in info["channels"]})
        if ctx.get("capture_channels") is not None and set(channels) != set(ctx["capture_channels"]):
            raise DeviceError("原始记录通道发生变化", "source_changed")
        self.update(ctx, channels=channels, require_full_scope=False)
        complete = all(info["complete"] for info in infos)
        if not complete and not allow_partial:
            raise DeviceError("记录不完整，未开始处理", "incomplete_recording")
        is_command = isinstance(profile, LocalCommand)
        definitions = () if is_command else profile.result_metrics(channels)
        fingerprint = (
            infos[0]["fingerprint"]
            if len(records) == 1
            else digest([(record.id, info["fingerprint"]) for record, info in zip(records, infos)])
        )
        quality = "simulated" if any(info["quality"] == "simulated" for info in infos) else "good"
        cache_identity = [anchor.id, fingerprint, profile.id, profile.version, effective, allow_partial]
        if is_command:
            cache_identity.append(profile.argv)
        key = digest(cache_identity)
        self.check(ctx)
        if not force:
            for saved in self.store.results(anchor):
                if saved["cache_key"] == key and saved["complete"]:
                    self.update(ctx, phase="reused", result_id=saved["id"])
                    return {
                        "result_id": saved["id"],
                        **source_reference,
                        "reused": True,
                        "complete": saved["complete"],
                    }
        self.update(ctx, phase="processing", progress={"done": 0, "total": sum(i["frames"] for i in infos)})
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
            for record in records:
                self.check(ctx)
                for frame in iter_source_frames(record.device):
                    self.check(ctx)
                    yield frame

        source = MetricInput(
            anchor.id,
            deepcopy(anchor.device.metadata),
            complete,
            frames,
            lambda: self.check(ctx),
            progress,
            recording_ids=recording_ids,
        )
        if is_command:
            metrics = profile.process(source, params)
        else:
            metrics = self._declared_metrics(profile.process(source, params), definitions)
        if quality == "simulated":
            for metric in metrics.values():
                if metric["value"] is not None:
                    metric["quality"] = "simulated"
        self.check(ctx)
        for record, info in zip(records, infos):
            if self.store.fingerprint(record, ctx["cancel"]) != info["fingerprint"]:
                raise DeviceError("计算期间原始数据变化，结果未发布", "source_changed")
        result = {
            "schema_version": 1,
            "id": uuid4().hex,
            **source_reference,
            "device_id": anchor.device_id,
            "identity": anchor.device.metadata["identity"],
            "channels": channels,
            "source_fingerprint": fingerprint,
            "profile_id": profile.id,
            "profile_version": profile.version,
            "parameters": effective,
            "cache_key": key,
            "completed_at": utc_now(),
            "complete": complete,
            "quality": quality,
            "metrics": metrics,
        }
        self.update(ctx, phase="publishing")
        with self.service.lock:
            self.check(ctx)
            self.service.repository.audit(
                "agent_metric_publishing", task_id=task["id"], result_id=result["id"]
            )
            self.store.publish(anchor, result)
            try:
                self.update(ctx, phase="completed", result_id=result["id"])
            except Exception:
                task.pop("result_id", None)
                task["phase"] = "failed"
                self.store.invalidate(anchor, result["id"])
                raise
        return {
            "result_id": result["id"],
            **source_reference,
            "reused": False,
            "complete": complete,
        }

    @staticmethod
    def _declared_metrics(measured, definitions) -> dict:
        if not isinstance(measured, dict) or set(measured) != {m.name for m in definitions}:
            raise ValueError("处理器返回的指标与声明不一致")
        metrics = {}
        for definition in definitions:
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
            metrics[definition.name] = {"value": item.value, "unit": definition.unit, "quality": item.quality}
        return metrics

    def close(self):
        self.worker.shutdown(wait=True)
