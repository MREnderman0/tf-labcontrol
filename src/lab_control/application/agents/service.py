"""Shared experiment facade. All physical I/O stays in the existing session workers."""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from lab_control.application.agents.policy import AgentPolicy, fail
from lab_control.domain.instruments import (
    AcquisitionRequest,
    DeviceCommand,
    DeviceError,
    MeasurementBatch,
    ValidationError,
    jsonable,
    utc_now,
)
from lab_control.domain.workspace import GENERAL
from lab_control.storage.agent_ledger import ACTIVE, AgentLedger


class AgentService:
    def __init__(self, runtime) -> None:
        self.runtime, self.repository = runtime, runtime.repository
        self.lock = threading.RLock()
        self.policy = AgentPolicy(self.repository, self.identity)
        self.ledger = AgentLedger(self.repository)
        self.previews: dict[str, dict] = {}
        self.active: dict[str, dict] = {}
        self.unjournaled: dict[str, dict] = {}
        self.worker = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent-task")
        self.closing = False
        self.server: Any = None
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self._watch, name="agent-expiry", daemon=True)
        self.thread.start()

    def identity(self, device_id: str) -> dict:
        record = next((r for r in self.runtime.inventory.all() if r["id"] == device_id), None)
        if record is None or record.get("identityConflict") or record.get("discoveryState") == "conflict":
            fail("设备不存在或身份存在冲突", "identity_conflict")
        value = record.get("identity", {})
        state: dict = next(
            (s for s in self.runtime.sessions.snapshots() if s["stableDeviceId"] == device_id), {}
        )
        value = state.get("identity") or value
        return {
            "manufacturer": value.get("manufacturer", ""),
            "model": value.get("model", ""),
            "serial": value.get("serial", ""),
            "adapter": record.get("adapterId", ""),
            # A serial-less instrument cannot be silently followed to a new endpoint.
            "resource": "" if value.get("serial") else record["config"]["resource"],
        }

    def _check(self, client: str, grant: str, device: str, action: str, **kwargs) -> dict:
        if self.closing or self.runtime.closed:
            fail("软件正在退出", "runtime_closing")
        return self.policy.check(client, grant, device, action, **kwargs)

    def _state(self, device: str) -> dict:
        return next((s for s in self.runtime.sessions.snapshots() if s["stableDeviceId"] == device), {})

    def _public_state(self, device: str) -> dict:
        state = self._state(device)
        # Legacy status dictionaries may mix values from several channels.
        # Readbacks/measurements use their explicit, authorized channel APIs.
        state.pop("lastStatus", None)
        return state

    def get_server_info(self, client: str) -> dict:
        self.policy.require_client(client)
        grants = [g for g in self.policy.snapshot()["grants"] if g["client_id"] == client]
        return {
            "name": "TF LabControl",
            "api_version": 1,
            "simulation": not self.runtime.hardware,
            "grants": grants,
            "output_on_stop": "unchanged_or_unknown",
        }

    def list_devices(self, client: str, grant_id: str) -> list[dict]:
        grant = self.policy.grants.get(grant_id, {})
        result = []
        for device in grant.get("devices", {}):
            self._check(client, grant_id, device, "view")
            result.append(
                {"device_id": device, "identity": self.identity(device), "state": self._public_state(device)}
            )
        if not result:
            fail("无可用实验授权", "grant_expired")
        return result

    def get_device_state(self, client: str, grant_id: str, device_id: str) -> dict:
        self._check(client, grant_id, device_id, "view")
        return {
            "device_id": device_id,
            "identity": self.identity(device_id),
            "session": self._public_state(device_id),
            "monitoring": self.runtime.monitoring.snapshots().get(device_id, {}),
            "cached": True,
        }

    def get_capabilities(self, client: str, grant_id: str, device_id: str) -> dict:
        from lab_control.application.controls import data_sources
        from lab_control.instruments.registry import DEVICE_DEFINITIONS

        self._check(client, grant_id, device_id, "view")
        snapshot = self.runtime.sessions.capability_snapshot(device_id)
        if snapshot is None:
            fail("请先连接设备以获取通道与实际范围", "disconnected")
        config = self.runtime.sessions.sessions[device_id].config
        definition = DEVICE_DEFINITIONS[config.adapter]
        return {
            "snapshot": jsonable(snapshot),
            "data_sources": jsonable(data_sources(config)),
            "acquisition_defaults": {
                "initiate": definition.acquisition_initiate_default,
                "requires_output_authority": definition.acquisition_requires_confirmation,
            },
            "effects": {
                str(channel): {
                    c.name: {
                        "read": "device_io" if c.readable else "unsupported",
                        "write": "output_trigger" if c.requires_confirmation else "configuration",
                    }
                    for c in caps.operations
                }
                for channel, caps in snapshot.channels.items()
            },
            "acquisition_effect": "device_operation_unless_model_verified",
        }

    def _capability(self, device: str, operation: str, channel: int):
        snapshot = self.runtime.sessions.capability_snapshot(device)
        if snapshot is None or snapshot.stale or channel not in snapshot.channels:
            fail("设备能力不可用或已过期，请在软件内刷新", "capability_stale")
        return snapshot.channels[channel].get(operation)

    def _guard(self, client, grant, device, action, *, expected=None, **scope):
        def guard(session) -> None:
            self._check(client, grant, device, action, **scope)
            if session.control_group:
                fail("本地界面正在应用一组参数，请等待完成", "task_busy")
            if expected is not None and (session.session_id, session.configuration_version) != expected:
                fail("会话或参数版本已变化，请重新读取并预览", "configuration_conflict")

        return guard

    def _task(self, client, grant, device, action, request_id, payload, start) -> dict:
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValidationError("request_id 必须为 1..128 字符的唯一标识")
        fingerprint = self.ledger.fingerprint([grant, device, action, payload])
        with self.lock:
            self._check(client, grant, device, action)
            previous = self.ledger.find_request(client, request_id, fingerprint)
            if previous:
                return previous
            if action != "stop" and (
                len(self.active) >= 128
                or sum(
                    ctx["task"]["device_id"] == device
                    and ctx["task"]["action"] not in {"monitor", "record", "stop"}
                    for ctx in self.active.values()
                )
                >= 4
            ):
                fail("活动任务已达上限", "task_busy")
            request = payload.get("request", payload)
            channels = request.get("channels", [1]) if action in {"acquire", "monitor", "record"} else []
            if action == "apply":
                channels = [self.previews.get(payload["preview_id"], {}).get("channel", 1)]
            if action == "stop":
                channels = self._lookup_task(payload["task_id"]).get("channels", [])
            self._check(client, grant, device, action, channels=tuple(channels))
            task = {
                "id": uuid4().hex,
                "client_id": client,
                "grant_id": grant,
                "device_id": device,
                "action": action,
                "request_id": request_id,
                "state": "accepted",
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "result": {},
                "error": "",
                "code": "",
                "channels": channels,
            }
            audit = self.repository.audit_sink.try_record if action == "stop" else self.repository.audit
            audit(
                "agent_request_accepted",
                task=task,
                parameters=payload,
                identity=self.identity(device),
                before=self._state(device),
            )
            try:
                self.ledger.insert(task, fingerprint)
            except Exception as exc:
                if action != "stop":
                    raise
                # Journal failure must never prevent an authorized cleanup.
                task["persistence_error"] = str(exc)
                self.unjournaled[task["id"]] = task
            context = {"task": task, "future": None, "monitor": None, "recorder": None, "stop": None}
            self.active[task["id"]] = context
            try:
                start(context)
                task["state"] = "running"
            except Exception as exc:
                self._finish(context, exc=exc)
            else:
                self._persist(context)
            self.wake.set()
            return deepcopy(task)

    def connect_device(self, client, grant_id, device_id, request_id, configure_acquisition=False):
        def start(ctx):
            self._check(client, grant_id, device_id, "connect")
            if configure_acquisition:
                self._check(client, grant_id, device_id, "acquisition_setup")
            state = self._state(device_id)
            if state:
                if state.get("connectionState") not in {"connected", "busy"}:
                    fail("已有会话不可用，请显式断开后重连", "disconnected")
                session = self.runtime.sessions.sessions[device_id]
                if configure_acquisition and not session.acquisition_setup_enabled:
                    fail("现有只读会话未启用采集初始化，请先断开后重新连接", "acquisition_setup_required")
                future: Future = Future()
                future.set_result({"reused": True, "session": self._public_state(device_id)})
                ctx["future"] = future
            else:

                def admission(identity):
                    grant = self._check(client, grant_id, device_id, "connect")
                    if configure_acquisition:
                        self._check(client, grant_id, device_id, "acquisition_setup")
                    if identity is not None:
                        expected = grant["devices"][device_id]
                        if any(
                            getattr(identity, key).strip().casefold() != expected[key].strip().casefold()
                            for key in ("manufacturer", "model", "serial")
                        ):
                            fail("连接身份与实验授权不符", "identity_conflict")

                ctx["future"] = self.runtime.connect(
                    device_id, configure_acquisition=configure_acquisition, admission=admission
                )

        return self._task(
            client,
            grant_id,
            device_id,
            "connect",
            request_id,
            {"configure_acquisition": configure_acquisition},
            start,
        )

    def disconnect_device(self, client, grant_id, device_id, request_id):
        def run():
            self._check(client, grant_id, device_id, "disconnect")
            self.runtime.sessions.disconnect(device_id, drain_reads=True)
            return {"disconnected": True, "output_state": "unchanged_or_unknown"}

        return self._task(
            client,
            grant_id,
            device_id,
            "disconnect",
            request_id,
            {},
            lambda ctx: ctx.update(future=self.worker.submit(run)),
        )

    def read_parameters(self, client, grant_id, device_id, operations, channel=1):
        self._check(client, grant_id, device_id, "read_parameters", channels=(channel,))
        if not operations or len(operations) > 32:
            raise ValidationError("每次读取 1..32 项参数")
        for operation in operations:
            if not self._capability(device_id, operation, channel).readable:
                fail("参数不支持读回", "unsupported")
        if self._state(device_id).get("pending", 0) >= 4:
            fail("设备正在处理请求，请稍后读取", "task_busy")
        future = self.runtime.sessions.submit(
            device_id,
            "read_settings",
            (tuple(operations), channel),
            before_io=self._guard(client, grant_id, device_id, "read_parameters", channels=(channel,)),
        )
        return future

    def preview_change(self, client, grant_id, device_id, operation, parameters, channel=1):
        self._check(client, grant_id, device_id, "apply", channels=(channel,), operation=operation)
        cap = self._capability(device_id, operation, channel)
        if not cap.writable:
            fail("参数只读", "unsupported")
        values = cap.validate(parameters)
        self._check(
            client, grant_id, device_id, "apply", channels=(channel,), operation=operation, parameters=values
        )
        if cap.requires_confirmation:
            self._check(client, grant_id, device_id, "output_trigger")
        state = self._state(device_id)
        preview = {
            "id": uuid4().hex,
            "client_id": client,
            "grant_id": grant_id,
            "device_id": device_id,
            "operation": operation,
            "parameters": values,
            "channel": channel,
            "session_id": state["sessionId"],
            "configuration_version": state["configurationVersion"],
            "requires_output_authority": cap.requires_confirmation,
            "expires_at": time.time() + 300,
        }
        with self.lock:
            self.previews = {k: p for k, p in self.previews.items() if p["expires_at"] > time.time()}
            if len(self.previews) >= 256:
                fail("未执行预览过多，请等待预览过期", "task_busy")
            self.previews[preview["id"]] = preview
        return deepcopy(preview)

    def apply_change(self, client, grant_id, device_id, preview_id, request_id):
        def start(ctx):
            preview = self.previews.get(preview_id)
            if (
                preview is None
                or preview["client_id"] != client
                or preview["grant_id"] != grant_id
                or preview["device_id"] != device_id
                or preview["expires_at"] <= time.time()
            ):
                fail("预览不存在或已过期，请重新预览", "preview_expired")
            scope = dict(
                channels=(preview["channel"],),
                operation=preview["operation"],
                parameters=preview["parameters"],
            )
            check = self._guard(
                client,
                grant_id,
                device_id,
                "apply",
                expected=(preview["session_id"], preview["configuration_version"]),
                **scope,
            )

            def guard(session):
                check(session)
                cap = self._capability(device_id, preview["operation"], preview["channel"])
                if cap.requires_confirmation:
                    self._check(client, grant_id, device_id, "output_trigger")
                self.repository.audit(
                    "agent_write_started",
                    task_id=ctx["task"]["id"],
                    client_id=client,
                    grant_id=grant_id,
                    preview=preview,
                    before=jsonable(session.settings.get((preview["channel"], preview["operation"]))),
                )

            ctx["future"] = self.runtime.sessions.submit(
                device_id,
                "apply",
                DeviceCommand(
                    preview["operation"], preview["parameters"], preview["channel"], confirmed=True
                ),
                before_io=guard,
            )

        return self._task(client, grant_id, device_id, "apply", request_id, {"preview_id": preview_id}, start)

    def _request(self, client, grant, device, action, value):
        from lab_control.instruments.registry import DEVICE_DEFINITIONS

        allowed = {
            "kind",
            "channels",
            "points",
            "quantity",
            "initiate",
            "waveform_extent",
            "duration",
            "sample_rate",
        }
        if not isinstance(value, dict) or value.keys() - allowed:
            raise ValidationError("采集请求含未知字段；确认权限由服务器管理")
        definition = DEVICE_DEFINITIONS[self.identity(device)["adapter"]]
        request = AcquisitionRequest(
            **{
                "initiate": definition.acquisition_initiate_default,
                **value,
                "channels": tuple(value.get("channels", [1])),
                "confirmed": True,
            }
        )
        request.validate()
        scope = self._check(client, grant, device, action, channels=request.channels)
        if (
            request.initiate
            or request.waveform_extent == "memory"
            or definition.acquisition_requires_confirmation
        ):
            self._check(client, grant, device, "output_trigger")
        return replace(request, allow_setup="acquisition_setup" in scope["actions"])

    def acquire_once(self, client, grant_id, device_id, request, request_id):
        def start(ctx):
            acquisition = self._request(client, grant_id, device_id, "acquire", request)
            context = self.repository.new_save_context(
                GENERAL,
                "single_frame",
                device_id=device_id,
                identity=self.runtime.sessions.sessions[device_id].identity,
            )

            def guard(session):
                self._request(client, grant_id, device_id, "acquire", request)
                self.repository.audit("agent_acquisition_started", task_id=ctx["task"]["id"])

            read = self.runtime.sessions.submit(
                device_id,
                "observe" if acquisition.kind == "status" else "acquire",
                acquisition,
                before_io=guard,
            )

            def save():
                batch = read.result()
                self.runtime.monitoring._validate_saved_batch(batch)
                folder = self.repository.export(batch, GENERAL, context=context)
                ctx["folder"] = str(folder)
                return self._batch_summary(batch)

            ctx["future"] = self.worker.submit(save)

        return self._task(client, grant_id, device_id, "acquire", request_id, request, start)

    def start_monitoring(
        self, client, grant_id, device_id, request, request_id, interval=0.5, recording=False
    ):
        action = "record" if recording else "monitor"

        def start(ctx):
            acquisition = self._request(client, grant_id, device_id, action, request)
            monitoring = self.runtime.monitoring
            # Atomic with GUI starts. Never merge/replace another experiment implicitly.
            with monitoring.lock:
                existing = monitoring.tasks.get(device_id)
                if existing and (
                    existing.running
                    or existing.pending
                    or existing.write_pending
                    or existing.recording_preparation
                    or (existing.recorder and not existing.recorder.binary_done)
                ):
                    fail("设备已有监测或记录；请查看任务并明确停止后再启动", "task_conflict")
                ctx["future"] = monitoring.start(
                    device_id,
                    (acquisition,),
                    interval=interval,
                    record_interval=None,
                    mode=GENERAL,
                    recording=recording,
                    background=True,
                )
                ctx["monitor"] = monitoring.tasks[device_id]

                def before_acquire(session, current):
                    from lab_control.instruments.registry import DEVICE_DEFINITIONS

                    self._guard(client, grant_id, device_id, action, channels=current.channels)(session)
                    if (
                        current.initiate
                        or current.waveform_extent == "memory"
                        or DEVICE_DEFINITIONS[session.config.adapter].acquisition_requires_confirmation
                    ):
                        self._check(client, grant_id, device_id, "output_trigger")
                    if current.allow_setup:
                        self._check(client, grant_id, device_id, "acquisition_setup")

                ctx["monitor"].before_acquire = before_acquire
                ctx["task"]["monitoring_context_id"] = ctx["monitor"].monitoring_context_id

        return self._task(
            client, grant_id, device_id, action, request_id, {"request": request, "interval": interval}, start
        )

    def start_recording(self, client, grant_id, device_id, request, request_id, interval=0.5):
        return self.start_monitoring(client, grant_id, device_id, request, request_id, interval, True)

    def get_latest_measurement(self, client, grant_id, device_id, kind="scalar", quantity="", channel=1):
        self._check(client, grant_id, device_id, "view", channels=(channel,))
        frame = self.runtime.monitoring.latest(device_id, kind, quantity, channel)
        if frame is None:
            return {"available": False}
        return {
            "available": True,
            "sequence": frame.sequence,
            "session_id": frame.session_id,
            "configuration_version": frame.configuration_version,
            "age_seconds": time.perf_counter() - frame.received_at,
            **self._batch_summary(
                replace(
                    frame.batch,
                    measurements=tuple(m for m in frame.batch.measurements if m.channel == channel),
                )
            ),
        }

    @staticmethod
    def _batch_summary(batch: MeasurementBatch) -> dict:
        import numpy as np

        return {
            "timestamp": batch.timestamp,
            "time_basis": batch.time_basis,
            "validation": batch.validation,
            "measurements": [
                {
                    "quantity": m.quantity,
                    "unit": m.unit,
                    "channel": m.channel,
                    "quality": str(m.quality),
                    "points": int(np.size(m.values)),
                    "preview": jsonable(np.asarray(m.values).reshape(-1)[:16]),
                }
                for m in batch.measurements
            ],
        }

    def get_task_status(self, client, grant_id, task_id):
        task = self._lookup_task(task_id)
        self._check(client, grant_id, task["device_id"], "view", channels=tuple(task.get("channels", [])))
        with self.lock:
            ctx = self.active.get(task_id)
            if ctx and ctx.get("monitor") and ctx["monitor"].recorder:
                task["recording"] = ctx["monitor"].recorder.snapshot()
        return task

    def list_tasks(self, client, grant_id):
        devices = {d["device_id"] for d in self.list_devices(client, grant_id)}
        tasks = [t for t in self.ledger.tasks() if t["device_id"] in devices]
        contexts = {t.get("monitoring_context_id") for t in tasks if t["state"] in ACTIVE}
        with self.runtime.monitoring.lock:
            for device, monitor in self.runtime.monitoring.tasks.items():
                if device in devices and monitor.running and monitor.monitoring_context_id not in contexts:
                    tasks.append(self._gui_task(device, monitor))
        allowed = []
        for task in tasks:
            try:
                self._check(
                    client, grant_id, task["device_id"], "view", channels=tuple(task.get("channels", []))
                )
            except DeviceError:
                continue
            allowed.append(task)
        return allowed

    @staticmethod
    def _gui_task(device, monitor):
        return {
            "id": "gui-" + monitor.monitoring_context_id,
            "device_id": device,
            "origin": "local_ui",
            "state": "running" if monitor.running else "stopping",
            "action": "record" if monitor.recording else "monitor",
            "monitoring_context_id": monitor.monitoring_context_id,
            "recording": monitor.recorder.snapshot() if monitor.recorder else {},
            "channels": sorted({channel for request in monitor.requests for channel in request.channels}),
        }

    def _lookup_task(self, task_id):
        with self.lock:
            if task_id in self.active:
                return deepcopy(self.active[task_id]["task"])
            if task_id in self.unjournaled:
                return deepcopy(self.unjournaled[task_id])
        if task_id.startswith("gui-"):
            with self.runtime.monitoring.lock:
                for device, monitor in self.runtime.monitoring.tasks.items():
                    if "gui-" + monitor.monitoring_context_id == task_id:
                        return self._gui_task(device, monitor)
            fail("本地任务已结束或被替换", "task_not_found")
        return self.ledger.get(task_id)

    def stop_task(self, client, grant_id, task_id, request_id):
        target = self._lookup_task(task_id)
        device = target["device_id"]

        def start(ctx):
            with self.lock:
                original = self.active.get(task_id)
                if original:
                    self._stop(original)
                    ctx["target"] = task_id
                else:
                    with self.runtime.monitoring.lock:
                        monitor = self.runtime.monitoring.tasks.get(device)
                        if (
                            task_id.startswith("gui-")
                            and monitor
                            and "gui-" + monitor.monitoring_context_id == task_id
                        ):
                            ctx["future"] = self.runtime.monitoring.stop(device)
                        else:
                            done: Future = Future()
                            done.set_result({"already_finished": True, "task_id": task_id})
                            ctx["future"] = done

        return self._task(client, grant_id, device, "stop", request_id, {"task_id": task_id}, start)

    def _stop(self, ctx):
        task = ctx["task"]
        if task["state"] == "stopping":
            return
        monitor = ctx.get("monitor")
        if monitor is not None:
            with self.runtime.monitoring.lock:
                if self.runtime.monitoring.tasks.get(task["device_id"]) is monitor:
                    ctx["stop"] = self.runtime.monitoring.stop(task["device_id"])
        # Single accepted commands are not cancellable; continue tracking completion.
        task["state"] = "stopping"
        self._persist(ctx)

    def _persist(self, ctx):
        ctx["task"]["updated_at"] = utc_now()
        history = ctx["task"].setdefault("phase_history", [])
        if not history or history[-1]["state"] != ctx["task"]["state"]:
            history.append({"state": ctx["task"]["state"], "at": ctx["task"]["updated_at"]})
        try:
            self.ledger.update(ctx["task"])
            return True
        except Exception as exc:
            ctx["task"]["persistence_error"] = str(exc)
            self.repository.audit_sink.try_record(
                "agent_journal_failed", task_id=ctx["task"]["id"], error=str(exc)
            )
            return False

    def _finish(self, ctx, result=None, exc=None):
        task = ctx["task"]
        if exc is not None:
            task.update(state="failed", error=str(exc), code=getattr(exc, "code", "operation_failed"))
            if hasattr(exc, "outcome"):
                task.update(state="partial", result=jsonable(exc.outcome))
        else:
            outcome = getattr(result, "outcome", "success")
            state = (
                "completed"
                if outcome == "success"
                else "partial"
                if outcome in {"partial", "uncertain"}
                else "failed"
            )
            task.update(
                state=state,
                result=jsonable(result) if result is not None else {},
                error=getattr(result, "error", ""),
                code="" if state == "completed" else outcome,
            )
        if ctx.get("folder"):
            try:
                self._register_artifacts(task, Path(ctx["folder"]))
            except Exception as artifact_error:
                task.update(state="partial", code="artifact_registration_failed", error=str(artifact_error))
        ctx["terminal"] = True
        if not self._persist(ctx):
            return
        self.repository.audit_sink.try_record("agent_task_finished", task=task)
        self.active.pop(task["id"], None)

    def _register_artifacts(self, task, folder):
        root = folder.resolve()
        if root.is_file():
            paths, root = [root], root.parent
        else:
            paths = list(root.rglob("*"))
        for path in paths:
            resolved = path.resolve()
            if path.is_file() and resolved.is_relative_to(root) and not path.is_symlink():
                artifact_id = self.ledger.fingerprint([task["id"], str(resolved)])[:32]
                self.ledger.save_artifact(
                    {
                        "id": artifact_id,
                        "task_id": task["id"],
                        "device_id": task["device_id"],
                        "path": str(resolved),
                        "root": str(root),
                        "name": path.relative_to(root).as_posix(),
                        "size": path.stat().st_size,
                        "channels": task.get("channels", []),
                    }
                )

    def list_artifacts(self, client, grant_id, task_id=""):
        result = []
        for artifact in self.ledger.artifacts(task_id):
            try:
                self._check(
                    client,
                    grant_id,
                    artifact["device_id"],
                    "results",
                    channels=tuple(artifact.get("channels", [])),
                )
            except DeviceError:
                continue
            result.append({k: v for k, v in artifact.items() if k not in {"path", "root"}})
        return result

    def get_artifact(self, client, grant_id, artifact_id):
        value, path = self.artifact_path(client, grant_id, artifact_id)
        return {
            "id": value["id"],
            "name": value["name"],
            "size": path.stat().st_size,
            "download_path": f"/artifacts/{artifact_id}?grant_id={grant_id}",
        }

    def artifact_path(self, client, grant_id, artifact_id):
        value, path = self.ledger.artifact(artifact_id)
        self._check(
            client, grant_id, value["device_id"], "results", channels=tuple(value.get("channels", []))
        )
        return value, path

    def _tick(self):
        with self.lock:
            for ctx in list(self.active.values()):
                task = ctx["task"]
                try:
                    if ctx.get("terminal"):
                        if self._persist(ctx):
                            self.active.pop(task["id"], None)
                        continue
                    try:
                        self.policy.check(
                            task["client_id"], task["grant_id"], task["device_id"], task["action"]
                        )
                    except DeviceError:
                        self._stop(ctx)
                    if ctx.get("target"):
                        if ctx["target"] not in self.active:
                            self._finish(
                                ctx, {"stopped_task": ctx["target"], "output_state": "unchanged_or_unknown"}
                            )
                        continue
                    future = ctx["future"]
                    monitor = ctx.get("monitor")
                    if future is not None and future.done():
                        value = future.result()
                        ctx["future"] = None
                        if monitor is None:
                            self._finish(ctx, value)
                            continue
                    if monitor is not None:
                        recorder = monitor.recorder
                        if recorder:
                            ctx["folder"] = str(recorder.folder)
                            if recorder.binary_done and not ctx.get("binary_saved"):
                                ctx["binary_saved"] = True
                                task["state"] = "binary_saved"
                                self._persist(ctx)
                                if not self.closing:
                                    continue
                        if not monitor.running and not monitor.pending and not monitor.recording_preparation:
                            if recorder and not recorder.finished.done():
                                state = "converting" if recorder.binary_done else "stopping"
                                if task["state"] != state:
                                    task["state"] = state
                                    self._persist(ctx)
                            else:
                                if recorder:
                                    recorder.finished.result()
                                    if recorder.error or recorder.csv_error:
                                        from lab_control.domain.recording import RecordingError

                                        raise RecordingError(recorder.outcome())
                                if monitor.error:
                                    raise DeviceError(monitor.error, "monitor_failed")
                                self._finish(ctx, recorder.snapshot() if recorder else {"stopped": True})
                except Exception as exc:
                    self._finish(ctx, exc=exc)

    def _watch(self):
        while not self.closing:
            self.wake.wait(0.1)
            self.wake.clear()
            try:
                self._tick()
            except Exception as exc:
                self.repository.audit_sink.try_record("agent_tracking_failed", error=str(exc))

    def snapshot(self):
        return {
            **self.policy.snapshot(),
            "tasks": self.ledger.tasks(),
            "server": self.server.snapshot() if self.server else {"running": False},
        }

    def stop_local(self, task_id):
        """Local operator can always request cleanup, even after grant expiry."""
        with self.lock:
            ctx = self.active.get(task_id)
            if ctx:
                self._stop(ctx)
        self.wake.set()

    def close(self):
        self.closing = True
        errors = []
        if self.server:
            try:
                self.server.close()
            except Exception as exc:
                errors.append(exc)
        self.wake.set()
        self.thread.join()
        with self.lock:
            for ctx in list(self.active.values()):
                try:
                    self._stop(ctx)
                except Exception as exc:
                    errors.append(exc)
        self.worker.shutdown(wait=True)
        if errors:
            for secondary in errors[1:]:
                errors[0].add_note(str(secondary))
            raise errors[0]
        # Runtime closes monitoring next. Persist incomplete state now; finalize
        # after its drain, before repository closure, through finalize_close().

    def finalize_close(self):
        self._tick()
        with self.lock:
            for ctx in list(self.active.values()):
                self._finish(ctx, exc=DeviceError("退出后最终状态未确认", "outcome_unknown"))
