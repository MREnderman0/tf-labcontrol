"""Local administration and server-enforced experiment grants."""

from __future__ import annotations

import math
import threading
from copy import deepcopy
from typing import Any, NoReturn
from uuid import uuid4

from lab_control.domain.instruments import DeviceError, ValidationError, utc_now

INTERNAL_CLIENT = "internal"
COMMAND_CLIENT = "internal-command"
COMMAND_ACTIONS = frozenset({"view", "process", "results", "stop"})

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
        "process",
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
        self.grants = repository.load_workspace("agent/grants", {})
        # Templates survive a restart, execution authority does not.
        for grant in self.grants.values():
            grant["enabled"] = False
        self._save()

    def _save(self) -> None:
        self.repository.save_workspace("agent/grants", self.grants)

    def require_client(self, client_id: str) -> None:
        if client_id != INTERNAL_CLIENT:
            fail("此调用方不适用于内部接入", "unauthorized")

    def create_grant(
        self,
        client_id: str,
        device_ids: list[str],
        actions: list[str],
        *,
        channels: list[int],
        operations: list[str],
        limits: dict[str, Any],
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
        return self._store_grant(
            {
                "client_id": client_id,
                "devices": identities,
                "actions": sorted(set(actions)),
                "channels": sorted(set(channels)),
                "operations": sorted(set(operations)),
                "limits": deepcopy(limits),
                "asset_confirmed": asset_confirmed,
            }
        )

    def create_full_grant(self, identities: dict[str, dict]) -> str:
        if not identities:
            raise ValidationError("没有可全部授权的设备，请先核对受支持型号和序列号")
        return self._store_grant(
            {
                "client_id": INTERNAL_CLIENT,
                "devices": identities,
                "actions": sorted(ACTIONS),
                "channels": [],
                "operations": [],
                "limits": {},
                "asset_confirmed": False,
                "full_device_access": True,
            }
        )

    def _store_grant(self, scope: dict) -> str:
        with self.lock:
            grant_id = uuid4().hex
            grant = {**scope, "id": grant_id, "enabled": True, "created_at": utc_now(), "expires_at": None}
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

    def require_grant(self, client_id: str, grant_id: str) -> dict:
        """Validate collection queries even when no matching assets are present."""
        self.require_client(client_id)
        with self.lock:
            grant = self.grants.get(grant_id, {})
            if grant.get("client_id") != client_id or not grant.get("enabled"):
                fail("实验授权已失效，请在软件中重新授权", "grant_expired")
            if "view" not in grant["actions"]:
                fail("查看设备未获授权")
            return deepcopy(grant)

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
        require_full_scope: bool = False,
    ) -> dict:
        if client_id == COMMAND_CLIENT:
            if grant_id or action not in COMMAND_ACTIONS:
                fail("临时命令入口仅支持处理及结果操作")
            self.identity_lookup(device)
            return {"channels": [], "full_device_access": True}
        self.require_client(client_id)
        with self.lock:
            grant = self.grants.get(grant_id, {})
            if grant.get("client_id") != client_id or not grant.get("enabled"):
                fail("实验授权已失效，请在软件中重新授权", "grant_expired")
            if device not in grant["devices"] or action not in grant["actions"]:
                fail("设备或操作超出实验授权")
            if not grant.get("full_device_access"):
                if require_full_scope or set(channels) - set(grant["channels"]):
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
            return {"grants": deepcopy(list(self.grants.values()))}
