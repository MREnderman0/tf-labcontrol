"""Shared experiment facade. All physical I/O stays in the existing session workers."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from lab_control.application.agents.command_processing import LOCAL_COMMAND_PROFILE
from lab_control.application.agents.policy import COMMAND_CLIENT, AgentPolicy, fail
from lab_control.domain.instruments import (
    AcquisitionRequest,
    DeviceCommand,
    DeviceError,
    MeasurementBatch,
    ValidationError,
    jsonable,
    utc_now,
)
from lab_control.domain.workspace import CATEGORY_LABELS, GENERAL, WORKSPACE_MODES
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
        from lab_control.application.agents.metrics import MetricTasks

        self.metrics = MetricTasks(self)
        self.closing = False
        self.server: Any = None
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self._watch, name="agent-tracking", daemon=True)
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
        grants = [g for g in self.policy.snapshot()["grants"] if g["client_id"] == client and g["enabled"]]
        return {
            "name": "TF LabControl",
            "api_version": 1,
            "simulation": not self.runtime.hardware,
            "grants": grants,
            "output_on_stop": "unchanged_or_unknown",
        }

    def grant_all_devices(self, template: dict | None = None) -> tuple[str, int, int]:
        """Sign one shared grant using the current verified asset identities."""
        from lab_control.instruments.registry import DEVICE_DEFINITIONS

        if self.closing or self.runtime.closed:
            fail("软件正在退出", "runtime_closing")
        expected = template["devices"] if template is not None else None
        with self.runtime.sessions.lock, self.runtime.inventory.lock:
            records = self.runtime.inventory.all()
            identities = {}
            for record in records:
                device = record["id"]
                if record.get("adapterId") not in DEVICE_DEFINITIONS or (
                    expected is not None and device not in expected
                ):
                    continue
                try:
                    identity = self.identity(device)
                except DeviceError as exc:
                    if exc.code != "identity_conflict":
                        raise
                    continue
                if (
                    not identity["manufacturer"].strip()
                    or not identity["model"].strip()
                    or identity["serial"].strip().upper() in {"", "0", "N/A", "UNKNOWN"}
                    or (expected is not None and identity != expected[device])
                ):
                    continue
                identities[device] = identity
            grant_id = self.policy.create_full_grant(identities)
        total = len(records) if expected is None else len(expected)
        return grant_id, len(identities), total - len(identities)

    def get_processing_capabilities(self, client, grant_id, device_id, *, local_commands=False):
        return self.metrics.capabilities(client, grant_id, device_id, local_commands=local_commands)

    def list_recordings(self, client, grant_id, device_id=""):
        return self.metrics.list_recordings(client, grant_id, device_id)

    def start_processing(
        self,
        client,
        grant_id,
        device_id,
        request_id,
        recording_id,
        profile_id,
        parameters=None,
        allow_partial=False,
        force=False,
    ):
        return self.metrics.start(
            client,
            grant_id,
            device_id,
            request_id,
            profile_id,
            parameters or {},
            recording_id=recording_id,
            allow_partial=allow_partial,
            force=force,
        )

    def start_experiment(
        self,
        client,
        grant_id,
        device_id,
        request_id,
        profile_id,
        capture_parameters=None,
        parameters=None,
        recording_ids=None,
        command=None,
        *,
        local_commands=False,
    ):
        if profile_id == LOCAL_COMMAND_PROFILE:
            if not local_commands:
                fail("请在服务端桌面启用临时命令处理", "local_command_disabled")
            if command is None or recording_ids is None:
                raise ValidationError("local.command 需要 command 和 recording_ids")
            client, grant_id = COMMAND_CLIENT, ""
        elif command is not None:
            raise ValidationError("command 仅用于 local.command")
        if recording_ids is not None and capture_parameters is not None:
            raise ValidationError("recording_ids 与 capture_parameters 只能选一种")
        return self.metrics.start(
            client,
            grant_id,
            device_id,
            request_id,
            profile_id,
            parameters or {},
            capture_parameters=(capture_parameters or {}) if recording_ids is None else None,
            recording_ids=recording_ids,
            command=command,
        )

    def list_processing_results(self, client, grant_id, device_id="", recording_id="", profile_id=""):
        return self.metrics.list_results(client, grant_id, device_id, recording_id, profile_id)

    def get_processing_result(self, client, grant_id, result_id, *, local_commands=False):
        return self.metrics.get_result(client, grant_id, result_id, local_commands=local_commands)

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

    def list_slots(
        self, client: str, grant_id: str, category: str | None = None, workspace: str = GENERAL
    ) -> list[dict]:
        """Read logical bindings without discovering, connecting or querying hardware."""
        if workspace not in WORKSPACE_MODES or (category is not None and category not in CATEGORY_LABELS):
            raise ValidationError("未知设备类别或工作区")
        if self.closing or self.runtime.closed:
            fail("软件正在退出", "runtime_closing")
        # Match bind_slot's lock order. Definitions, identity and binding must
        # describe one snapshot; keep revocation serialized until it is built.
        with self.runtime.sessions.lock, self.runtime.inventory.lock, self.policy.lock:
            grant = self.policy.require_grant(client, grant_id)
            result = []
            for record in self.runtime.inventory.all():
                device = record["id"]
                slot = record.get("slots", {}).get(workspace, "")
                slot_category = slot.partition(":")[0]
                if device not in grant["devices"] or not slot or (category and slot_category != category):
                    continue
                if slot_category not in CATEGORY_LABELS:
                    continue
                definitions = self.repository.function_slots.definitions(slot_category, workspace)
                if slot not in definitions:
                    continue
                self._check(client, grant_id, device, "view")
                identity = self.identity(device)
                binding = {
                    "workspace": workspace,
                    "slot_id": slot,
                    "device_id": device,
                    "identity": identity,
                }
                fingerprint = hashlib.sha256(
                    json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                        "utf-8"
                    )
                ).hexdigest()
                result.append(
                    {
                        **binding,
                        "category": slot_category,
                        "label": definitions[slot],
                        "state": self._public_state(device),
                        "cached": True,
                        "binding_fingerprint": fingerprint,
                    }
                )
            return sorted(result, key=lambda item: (item["category"], item["slot_id"]))

    def resolve_slot(self, client: str, grant_id: str, slot_id: str, workspace: str = GENERAL) -> dict:
        # An absent, unbound or unauthorized slot has the same public result.
        for slot in self.list_slots(client, grant_id, workspace=workspace):
            if slot["slot_id"] == slot_id:
                return slot
        fail("槽位不可用，请刷新并核对设备绑定与授权", "slot_unavailable")

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
        recorded_kinds = getattr(self.runtime.sessions.sessions[device_id].adapter, "recorded_run_kinds", ())
        sources = [
            jsonable(source)
            for source in data_sources(config)
            if source.request_defaults.get("duration") is None or source.kind in recorded_kinds
        ]
        for source in sources:
            source["request_defaults"].pop("confirmed", None)
        return {
            "snapshot": jsonable(snapshot),
            "data_sources": sources,
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
                or (
                    action != "process"
                    and sum(
                        ctx["task"]["device_id"] == device
                        and ctx["task"]["action"] not in {"monitor", "record", "stop", "process"}
                        for ctx in self.active.values()
                    )
                    >= 4
                )
            ):
                fail("活动任务已达上限", "task_busy")
            request = payload.get("request", payload)
            channels = request.get("channels", [1]) if action in {"acquire", "monitor", "record"} else []
            require_full_scope = False
            if action == "apply":
                channels = [self.previews.get(payload["preview_id"], {}).get("channel", 1)]
            if action == "stop":
                target = self._lookup_task(payload["task_id"])
                channels = target.get("channels", [])
                require_full_scope = target.get("require_full_scope", False)
            self._check(
                client, grant, device, action, channels=tuple(channels), require_full_scope=require_full_scope
            )
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
            if require_full_scope:
                task["require_full_scope"] = True
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
            if acquisition.duration is not None:
                raise ValidationError("定时采集请使用 start_recording")
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
            if acquisition.duration is not None:
                if not recording:
                    raise ValidationError("定时采集请使用 start_recording")
                self._start_finite_recording(ctx, client, grant_id, device_id, acquisition, request)
                return
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

    def _start_finite_recording(self, ctx, client, grant, device, acquisition, request):
        from lab_control.storage.recording import read_frames

        sessions = self.runtime.sessions
        with sessions.lock:
            session = sessions.sessions.get(device)
            if session is None or acquisition.kind not in getattr(session.adapter, "recorded_run_kinds", ()):
                raise DeviceError("型号未提供此定时采集入口", "capture_unsupported")

            def guard(_session):
                self._request(client, grant, device, "record", request)
                if acquisition.allow_setup:
                    self._check(client, grant, device, "acquisition_setup")

            future = sessions.start_acquisition(device, acquisition, before_io=guard)
            recorder = session.direct_recording
            ctx.update(cancel=threading.Event(), direct_recording=recorder, folder=str(recorder.folder))

        def finish():
            paths = future.result()
            if ctx["cancel"].is_set():
                from lab_control.storage.processing import ProcessingCancelled

                raise ProcessingCancelled("定时采集已停止")
            if paths and recorder.committed:
                tail = list(read_frames(Path(paths[-1]), after=recorder.committed - 1))
                if len(tail) == 1 and tail[0]["sequence"] == recorder.committed:
                    config = tail[0]["configuration"]
                    if config.get("final") is True and config.get("complete") is True:
                        return {"complete": True, "frames": recorder.committed}
            raise DeviceError("定时采集缺少完整的结束帧", "incomplete_recording")

        published: Future = Future()
        ctx["future"] = published

        def completed(_future):
            try:
                published.set_result(finish())
            except Exception as exc:
                published.set_exception(exc)

        future.add_done_callback(completed)

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

    def _check_task_view(self, client, grant_id, task):
        self._check(
            client,
            grant_id,
            task["device_id"],
            "view",
            channels=tuple(task.get("channels", [])),
            require_full_scope=task.get("require_full_scope", False),
        )

    @staticmethod
    def _task_client(client, grant_id, task, local_commands):
        if (
            local_commands
            and not grant_id
            and (task.get("client_id") == COMMAND_CLIENT or task.get("profile_id") == LOCAL_COMMAND_PROFILE)
        ):
            return COMMAND_CLIENT
        return client

    def get_task_status(self, client, grant_id, task_id, *, local_commands=False):
        task = self._lookup_task(task_id)
        client = self._task_client(client, grant_id, task, local_commands)
        self._check_task_view(client, grant_id, task)
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
                self._check_task_view(client, grant_id, task)
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

    def stop_task(self, client, grant_id, task_id, request_id, *, local_commands=False):
        target = self._lookup_task(task_id)
        client = self._task_client(client, grant_id, target, local_commands)
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
        if "cancel" in ctx:
            ctx["cancel"].set()
            with self.runtime.sessions.lock:
                session = self.runtime.sessions.sessions.get(task["device_id"])
                recorder = ctx.get("direct_recording")
                if (
                    recorder is not None
                    and not recorder.binary_done
                    and session is not None
                    and session.direct_recording is recorder
                ):
                    self.runtime.sessions.stop_acquisition(task["device_id"])
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
            from lab_control.storage.processing import ProcessingCancelled

            if isinstance(exc, ProcessingCancelled):
                task.update(state="cancelled", code="cancelled")
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
            if task["action"] == "process" and isinstance(result, dict) and result.get("complete") is False:
                task.update(state="partial", code="incomplete_recording")
        if ctx.get("folder"):
            try:
                from lab_control.storage.layout import manifest
                from lab_control.storage.metric_results import digest

                metadata = manifest(Path(ctx["folder"]))
                if metadata.get("run_id") and metadata.get("device_id") == task["device_id"]:
                    task["recording_id"] = digest([task["device_id"], metadata["run_id"]])[:32]
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
        """Local operator can always request cleanup, even after revocation."""
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
        self.metrics.close()
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
