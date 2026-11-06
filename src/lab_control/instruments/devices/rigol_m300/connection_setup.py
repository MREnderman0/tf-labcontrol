"""User-authorized, identity-bound thermistor setup for formal M300 connections."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any, Callable

from lab_control.domain.instruments import DeviceCommand, DeviceError, ValidationError, utc_now

from .protocol import boolean, csv_numbers, measurement_kind, scan_channels
from .thermistor_curves import CURVES

if TYPE_CHECKING:
    from .adapter import M300Adapter


KIND = "m300-thermistor-scan"


def preflight_issues(before: dict, channels: dict) -> list[str]:
    """Describe known setup constraints without treating them as transport errors."""
    settings = before["settings"]
    issues = []
    if not set(scan_channels(settings["scan_list"])) <= set(channels):
        issues.append("扫描列表含未授权通道")
    if not settings["dmm_state"]:
        issues.append("DMM 未开启")
    if settings["monitor_state"]:
        issues.append("通道监视已开启")
    if settings["trigger_source"] != "IMM":
        issues.append(f"触发源为 {settings['trigger_source']}（需 IMM）")
    if settings["trigger_count"] != 1:
        issues.append(f"触发次数为 {settings['trigger_count']:g}（需 1）")
    fields = [key for key, value in settings["format"].items() if value]
    if fields:
        issues.append("读数附加字段已开启：" + "/".join(fields))
    if any(value for values in before["relays"].values() for value in values):
        issues.append("输入继电器尚未全部打开，可能仍在扫描")
    return issues


def validate_channels(channels: dict, unit: str) -> None:
    if (
        not isinstance(channels, dict)
        or not channels
        or unit not in {"C", "F", "K"}
        or any(
            type(ch) is not int
            or ch // 100 not in range(1, 6)
            or ch % 100 not in range(1, 33)
            or not isinstance(curve, str)
            or curve not in CURVES
            for ch, curve in channels.items()
        )
    ):
        raise ValidationError("M300 连接配置需要有效的 MC3132 通道、换温曲线 ID 和温度单位")


class ThermistorConnectionSetup:
    def __init__(self, adapter: M300Adapter, load: Callable, save: Callable):
        # Binding is storage-only. Discovery and read-only connections must never apply the plan.
        self.adapter, self.load, self.save = adapter, load, save

    def _identity(self) -> str:
        identity = self.adapter.identity
        if identity is None or not identity.key or not identity.firmware:
            raise DeviceError("保存的 M300 配置需要完整型号、序列号和固件核验", "identity_required")
        return identity.key

    def _modules(self, channels: dict, *, refresh: bool = True) -> dict[str, str]:
        if refresh:
            self.adapter.refresh_capabilities()
        if not set(channels) <= set(self.adapter.profile.channels):
            raise DeviceError("保存的扫描通道没有对应的 MC3132 模块", "not_configured")
        details = self.adapter.capability_details
        if not details.get("dmm_installed"):
            raise DeviceError("M300 未安装 DMM，未恢复配置", "not_configured")
        slots = {f"slot_{ch // 100 * 100}" for ch in channels}
        slots.update(
            key
            for key, value in details.items()
            if key.startswith("slot_") and value.split(",")[1].strip() == "MC3065"
        )
        return {slot: details[slot] for slot in sorted(slots)}

    def snapshot(self) -> dict[str, Any]:
        """Read all identified inputs so setup cannot silently change other channels."""
        adapter = self.adapter
        globals_ = {
            key: adapter.read(key)
            for key in (
                "scan_list",
                "scan_size",
                "trigger_source",
                "trigger_count",
                "trigger_interval",
                "dmm_state",
                "monitor_state",
            )
        }
        globals_["format"] = {
            key: boolean(adapter.transport.query(f"FORM:READ:{key}?"))
            for key in ("UNIT", "TIME", "CHAN", "ALAR")
        }
        inputs, relays = {}, {}
        for ch in adapter.profile.channels:
            if ch == 1:
                continue
            slot = ch // 100 * 100
            if str(slot) not in relays:
                values = csv_numbers(adapter.transport.query(f"ROUT:CLOS? (@{slot + 1}:{slot + 32})"))
                if len(values) != 32 or any(value not in (0, 1) for value in values):
                    raise DeviceError("M300 继电器状态回复无效", "parse_error")
                relays[str(slot)] = values
            values_ = {key: adapter.read(key, ch) for key in ("configuration", "function")}
            if values_["function"] == "TEMP":
                values_["unit"] = adapter.read("temperature_unit", ch)
                values_["sensor"] = adapter.read("temperature_sensor", ch)
                if values_["sensor"] == "THER":
                    values_["nominal"] = adapter.read("thermistor_type", ch)
            inputs[str(ch)] = values_
        return dict(settings=globals_, inputs=inputs, relays=relays)

    def remember(self, channels: dict, *, unit: str, confirmed: bool) -> None:
        if not confirmed:
            raise DeviceError("需要明确授权保存 M300 自动连接配置", "confirmation_required")
        validate_channels(channels, unit)
        identity_key = self._identity()
        assert self.adapter.identity is not None
        modules = self._modules(channels)
        self.save(
            dict(
                schema_version=2,
                kind=KIND,
                enabled=True,
                identity_key=identity_key,
                firmware=self.adapter.identity.firmware,
                modules=modules,
                channels={str(ch): nominal for ch, nominal in sorted(channels.items())},
                unit=unit,
                authorized_at=utc_now(),
                last_attempt=None,
            )
        )

    def disable(self, *, confirmed: bool) -> None:
        if not confirmed:
            raise DeviceError("需要明确授权停用 M300 自动连接配置", "confirmation_required")
        state = deepcopy(self.load())
        if not state:
            return
        if state.get("identity_key") != self._identity() or state.get("kind") != KIND:
            raise DeviceError("保存配置与实际 M300 身份不符", "identity_conflict")
        state.update(enabled=False, disabled_at=utc_now())
        self.save(state)

    def _validate(self, state: dict) -> dict:
        if state.get("schema_version") != 2 or state.get("kind") != KIND:
            raise ValidationError("旧 M300 仪器直读温度配置已停用，请绑定电阻换温曲线")
        if state.get("identity_key") != self._identity():
            raise DeviceError("保存配置与实际 M300 型号/序列号不符", "identity_conflict")
        assert self.adapter.identity is not None
        if state.get("firmware") != self.adapter.identity.firmware:
            raise DeviceError("M300 固件已变化，需重新核验保存的配置", "qualification_stale")
        try:
            channels = {int(ch): nominal for ch, nominal in state["channels"].items()}
            if len(channels) != len(state["channels"]):
                raise ValueError("Duplicate channel")
            validate_channels(channels, state["unit"])
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ValidationError("保存的 M300 通道配置损坏") from exc
        return dict(sorted(channels.items()))

    def conversion(self) -> dict | None:
        """Query-free binding check, including read-only acquisition connections."""
        state = self.load()
        if state is None or (isinstance(state, dict) and state.get("enabled") is False):
            return None
        if not isinstance(state, dict):
            raise ValidationError("M300 电阻换温配置损坏")
        channels = self._validate(state)
        if not state.get("authorized_at") or state.get("enabled") is not True:
            raise DeviceError("M300 电阻换温配置缺少授权", "not_configured")
        if self._modules(channels, refresh=False) != state.get("modules"):
            raise DeviceError("M300 电阻换温配置的模块身份不符", "identity_conflict")
        return {"channels": {str(ch): curve for ch, curve in channels.items()}, "unit": state["unit"]}

    def prepare(self) -> None:
        state = deepcopy(self.load())
        if state is None or (isinstance(state, dict) and state.get("enabled") is False):
            return
        if not isinstance(state, dict) or state.get("enabled") is not True or not state.get("authorized_at"):
            raise ValidationError("M300 自动连接配置缺少明确授权")
        channels = self._validate(state)
        previous = state.get("last_attempt") or {}
        if previous.get("status") in {"writing", "failed"}:
            raise DeviceError(
                "上次 M300 配置恢复未确认，已禁止自动重放；请先核对实机状态并重新授权配置",
                "acquisition_setup_failed",
            )
        info: dict[str, Any] = dict(started_at=utc_now(), status="reading", operations=[])
        self.adapter.acquisition_preparation = info
        state["last_attempt"] = info

        def checkpoint() -> None:
            self.save(deepcopy(state))

        def write(operation: str, value: Any, channel: int = 1) -> None:
            info["status"] = "writing"
            entry = dict(operation=operation, value=value, channel=channel, attempted_at=utc_now())
            info["operations"].append(entry)
            # A crash or storage failure after this point must block automatic replay.
            checkpoint()
            result = self.adapter.apply(DeviceCommand(operation, {"value": value}, channel, True))
            entry.update(outcome=result.outcome, readback=result.readback, error=result.error)
            checkpoint()
            if result.outcome != "success":
                raise DeviceError(
                    f"M300 {operation}: {result.outcome}: {result.error}", "acquisition_setup_failed"
                )

        try:
            if self._modules(channels) != state["modules"]:
                raise DeviceError("M300 输入或 DMM 模块身份变化，未恢复配置", "identity_conflict")
            before = self.snapshot()
            info["before"] = before
            settings = before["settings"]
            scan = scan_channels(settings["scan_list"])
            if len(scan) != settings["scan_size"]:
                raise DeviceError("M300 扫描列表与通道数不一致", "parse_error")
            issues = preflight_issues(before, channels)
            if issues:
                info.update(
                    status="blocked",
                    issues=issues,
                    message="M300 采集待准备："
                    + "；".join(issues)
                    + "。请停止仪器扫描并调整上述设置，再重新连接。当前未修改仪器配置。",
                    finished_at=utc_now(),
                )
                # Identity and transport are valid. Keep the connection available,
                # but neither initialize nor acquire until an explicit reconnect.
                checkpoint()
                return
            checkpoint()
            for ch in channels:
                original = before["inputs"][str(ch)]
                if original["function"] != "RES":
                    write("function", "RES", ch)
            if scan != tuple(channels):
                write("scan_list", "(@" + ",".join(map(str, channels)) + ")")
            after = self.snapshot()
            info["after"] = after
            for ch in channels:
                actual = after["inputs"][str(ch)]
                if actual["function"] != "RES" or measurement_kind(actual["configuration"]) != (
                    "resistance",
                    "ohm",
                ):
                    raise DeviceError(f"M300 CH{ch} 电阻配置读回不一致", "readback_mismatch")
                if before["inputs"][str(ch)]["function"] == "RES" and actual != before["inputs"][str(ch)]:
                    raise DeviceError(f"M300 CH{ch} 原电阻设置发生变化", "readback_mismatch")
            if (
                scan_channels(after["settings"]["scan_list"]) != tuple(channels)
                or after["settings"]["scan_size"] != len(channels)
                or any(
                    after["settings"][key] != value
                    for key, value in settings.items()
                    if key not in {"scan_list", "scan_size"}
                )
                or before["relays"] != after["relays"]
                or any(
                    after["inputs"].get(ch) != value
                    for ch, value in before["inputs"].items()
                    if int(ch) not in channels
                )
            ):
                raise DeviceError("M300 扫描或未选通道/全局设置读回不一致", "readback_mismatch")
            info.update(status="ready", finished_at=utc_now())
            checkpoint()
            self.adapter._remember("remembered_scan_setup", dict(channels=list(channels), unit=state["unit"]))
        except Exception as exc:
            info.update(
                status="failed" if info["operations"] else "blocked", error=str(exc), finished_at=utc_now()
            )
            try:
                checkpoint()
            except Exception as save_error:
                info["journal_error"] = str(save_error)
            raise DeviceError(
                "M300 保存配置恢复未完成，未自动重试：" + str(exc), "acquisition_setup_failed"
            ) from exc
