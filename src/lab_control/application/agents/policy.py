"""Local administration and server-enforced experiment grants."""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import threading
import time
from copy import deepcopy
from typing import Any, NoReturn
from uuid import uuid4

from lab_control.domain.instruments import DeviceError, ValidationError, utc_now

ACTIONS = frozenset(
    {
        "view",
        "connect",
        "disconnect",
        "read_parameters",
        "apply",
        "acquire",
        "monitor",
        "record",
        "stop",
        "results",
        "acquisition_setup",
        "output_trigger",
    }
)


def fail(message: str, code: str = "permission_denied") -> NoReturn:
    raise DeviceError(message, code)


class AgentPolicy:
    """Only trusted local code receives this object; MCP never exposes admin methods."""

    def __init__(self, repository, identity_lookup) -> None:
        self.repository, self.identity_lookup = repository, identity_lookup
        self.lock = threading.RLock()
        self.clients = repository.load_workspace("agent/clients", {})
        self.grants = repository.load_workspace("agent/grants", {})
        # Templates survive a restart, execution authority does not.
        for grant in self.grants.values():
            grant["enabled"] = False
        self._save()

    def _save(self) -> None:
        self.repository.save_workspace("agent/clients", self.clients)
        self.repository.save_workspace("agent/grants", self.grants)

    def create_client(self, name: str) -> tuple[str, str]:
        if not name.strip() or len(name) > 100:
            raise ValidationError("客户端名称须为 1..100 字符")
        with self.lock:
            if len(self.clients) >= 128:
                raise ValidationError("客户端数量已达上限")
            client_id, token = uuid4().hex, secrets.token_urlsafe(32)
            self.repository.audit("agent_client_created", client_id=client_id, name=name)
            self.clients[client_id] = {
                "id": client_id,
                "name": name,
                "digest": hashlib.sha256(token.encode()).hexdigest(),
                "enabled": True,
                "created_at": utc_now(),
            }
            try:
                self._save()
            except Exception:
                self.clients.pop(client_id, None)
                raise
            return client_id, token

    def authenticate(self, token: str) -> str:
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            for client in self.clients.values():
                if hmac.compare_digest(client["digest"], digest) and client["enabled"]:
                    return client["id"]
        fail("客户端凭据无效或已撤销", "unauthorized")
        raise AssertionError

    def require_client(self, client_id: str) -> None:
        with self.lock:
            if not self.clients.get(client_id, {}).get("enabled"):
                fail("客户端已撤销", "unauthorized")

    def revoke_client(self, client_id: str) -> None:
        with self.lock:
            self.clients[client_id]["enabled"] = False
            for grant in self.grants.values():
                if grant["client_id"] == client_id:
                    grant["enabled"] = False
            self._save()
            self.repository.audit_sink.try_record("agent_client_revoked", client_id=client_id)

    def create_grant(
        self,
        client_id: str,
        device_ids: list[str],
        actions: list[str],
        *,
        channels: list[int],
        operations: list[str],
        limits: dict[str, Any],
        hours: float = 2,
        asset_confirmed: bool = False,
    ) -> str:
        self.require_client(client_id)
        if (
            not device_ids
            or not actions
            or set(actions) - ACTIONS
            or not channels
            or any(type(c) is not int or c < 1 for c in channels)
        ):
            raise ValidationError("请选择设备、操作权限和有效通道")
        if isinstance(hours, bool) or not math.isfinite(hours) or not 0 < hours <= 168:
            raise ValidationError("授权有效期须大于 0 且不超过 168 小时")
        if "apply" in actions and not operations:
            raise ValidationError("参数写入必须列出允许的能力名称")
        if not isinstance(limits, dict):
            raise ValidationError("参数范围必须是对象")
        for operation, parameters in limits.items():
            if operation not in operations or not isinstance(parameters, dict):
                raise ValidationError("范围必须对应允许的能力与参数")
            for bound in parameters.values():
                if (
                    not isinstance(bound, list)
                    or len(bound) != 2
                    or any(type(v) not in (float, int) or not math.isfinite(v) for v in bound)
                    or bound[0] > bound[1]
                ):
                    raise ValidationError("参数范围格式为 [最小值, 最大值]")
        identities = {}
        for device in device_ids:
            identity = self.identity_lookup(device)
            if not identity.get("model") or (not identity.get("serial") and not asset_confirmed):
                raise ValidationError("请先识别完整型号；无可读序列号时须在本机确认资产映射")
            identities[device] = identity
        with self.lock:
            grant_id = uuid4().hex
            grant = {
                "id": grant_id,
                "client_id": client_id,
                "devices": identities,
                "actions": sorted(set(actions)),
                "channels": sorted(set(channels)),
                "operations": sorted(set(operations)),
                "limits": deepcopy(limits),
                "expires_at": time.time() + hours * 3600,
                "enabled": True,
                "created_at": utc_now(),
                "asset_confirmed": asset_confirmed,
            }
            self.repository.audit("agent_grant_created", grant=grant)
            self.grants[grant_id] = grant
            try:
                self._save()
            except Exception:
                self.grants.pop(grant_id, None)
                raise
            return grant_id

    def revoke_grant(self, grant_id: str) -> None:
        with self.lock:
            self.grants[grant_id]["enabled"] = False
            self._save()
            self.repository.audit_sink.try_record("agent_grant_revoked", grant_id=grant_id)

    def check(
        self,
        client_id: str,
        grant_id: str,
        device: str,
        action: str,
        *,
        channels: tuple[int, ...] = (),
        operation: str = "",
        parameters: dict | None = None,
    ) -> dict:
        self.require_client(client_id)
        with self.lock:
            grant = self.grants.get(grant_id, {})
            if (
                grant.get("client_id") != client_id
                or not grant.get("enabled")
                or grant.get("expires_at", 0) <= time.time()
            ):
                fail("实验授权已失效，请在软件中重新授权", "grant_expired")
            if device not in grant["devices"] or action not in grant["actions"]:
                fail("设备或操作超出实验授权")
            if set(channels) - set(grant["channels"]):
                fail("通道超出实验授权")
            if operation and operation not in grant["operations"]:
                fail("参数能力未获授权")
            for name, value in (parameters or {}).items():
                bound = grant["limits"].get(operation, {}).get(name)
                if bound is not None:
                    values = value if isinstance(value, list) else [value]
                    if any(
                        type(v) not in (int, float) or not math.isfinite(v) or not bound[0] <= v <= bound[1]
                        for v in values
                    ):
                        fail("参数超出实验授权范围")
            expected = grant["devices"][device]
        if self.identity_lookup(device) != expected:
            fail("设备身份或资产映射已变化，请重新核对授权", "identity_conflict")
        return deepcopy(grant)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "clients": [{k: v for k, v in c.items() if k != "digest"} for c in self.clients.values()],
                "grants": deepcopy(list(self.grants.values())),
            }
