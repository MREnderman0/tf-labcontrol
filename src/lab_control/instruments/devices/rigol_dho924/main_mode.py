"""Explicit-session MAIN policy; never used by scans or read-only connections."""

from typing import TYPE_CHECKING, Any

from lab_control.domain.instruments import ControlUpdate, DeviceError, ParameterReading, utc_now

from .profile import MAIN_TIMEBASE_MAX
from .protocol import equivalent

if TYPE_CHECKING:
    from .adapter import DHO924Adapter


def prepare_main(adapter: "DHO924Adapter") -> ControlUpdate:
    if adapter._main_recovery_failed:
        raise DeviceError("退出 ROLL 的结果未确认，请重新连接后再试", "acquisition_setup_failed")
    before, _ = adapter.snapshot()
    before["1.trigger_mode"] = adapter.read("trigger_mode")
    report: dict[str, Any] = {"started_at": utc_now(), "before": before, "commands": [], "state": "pending"}
    adapter.capability_details["acquisition_setup"] = report
    adapter.invalidate_acquisition_context("apply")
    try:
        if before["1.timebase_mode"] == "ROLL":
            target = min(before["1.timebase"], MAIN_TIMEBASE_MAX)
            if target <= 0:
                raise DeviceError("无效的时基读回值", "parse_error")
            steps = []
            if before["1.timebase"] > MAIN_TIMEBASE_MAX:
                steps.append((f":TIM:MAIN:SCAL {target:g}", "timebase", target))
            steps.append((":TIM:MODE MAIN", "timebase_mode", "MAIN"))
            for payload, operation, expected in steps:
                report["commands"].append(payload)
                adapter.transport.write(payload)
                adapter.wait_setting_completion()
                actual = adapter.read(operation)
                report.setdefault("readbacks", {})[operation] = actual
                error = adapter.transport.query(":SYST:ERR?").decode("ascii").strip()
                if not error.lstrip("+").startswith("0,") or not equivalent(expected, actual):
                    raise DeviceError(
                        f"退出 ROLL 未确认：{operation} 请求 {expected}，读回 {actual}；{error}",
                        "acquisition_setup_failed",
                    )
        adapter._ranges_dirty = True
        adapter.refresh_capabilities()
        after = {key: adapter._settings[key] for key in before}
        report["after"] = after
        if (
            after["1.timebase_mode"] != "MAIN"
            or after["1.trigger_mode"] != before["1.trigger_mode"]
            or before["1.timebase_mode"] == "ROLL"
            and after["1.timebase"] > MAIN_TIMEBASE_MAX
        ):
            raise DeviceError("退出 ROLL 后的主时基或运行状态核验不一致", "acquisition_setup_failed")
        report.update(state="ready", finished_at=utc_now())
        return ControlUpdate(
            tuple(
                ParameterReading(
                    key.split(".", 1)[1], int(key.split(".", 1)[0]), value, "instrument", adapter._stamps[key]
                )
                for key, value in after.items()
            ),
            tuple((int(key.split(".", 1)[0]), key.split(".", 1)[1]) for key in after),
            True,
        )
    except Exception as exc:
        adapter._main_recovery_failed = True
        adapter.invalidate_acquisition_context("error")
        report.update(state="failed", error=str(exc), finished_at=utc_now())
        raise DeviceError(f"自动退出 ROLL 失败，未重复设置：{exc}", "acquisition_setup_failed") from exc
