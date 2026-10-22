"""E00 waveform lifecycle and durable command journal (never a hardware readback).

The vendor's standard-wave commands are write-only. Unknown state stays unknown
across connections; the journal is evidence for recovery, not an auto-resume job.
"""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any, Callable

from lab_control.domain.instruments import ApplyResult, DeviceError, ValidationError, jsonable, utc_now

from .protocol import build_standard_waveform, build_waveform_stop, validate_waveform


class WaveformControl:
    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter
        self.running: dict[int, bool | None] = {ch: None for ch in adapter.channels}
        self.journal: dict[str, Any] = {"schema_version": 1, "channels": {}}
        self.save: Callable[[dict], None] = lambda value: None
        self.owned = False
        self.normalizing = False

    def bind(self, load: Callable[[], Any], save: Callable[[dict], None]) -> None:
        try:
            stored = load()
        except (ValueError, TypeError):
            stored = None
        if isinstance(stored, dict) and stored.get("schema_version") == 1:
            self.journal = deepcopy(stored)
            if not isinstance(self.journal.get("channels"), dict):
                self.journal["channels"] = {}
            if not isinstance(self.journal.get("last_mode_at", ""), str):
                self.journal["last_mode_at"] = ""
        self.save = save
        # Even a clean shutdown record cannot prove what happened while disconnected.
        self.running = {ch: None for ch in self.adapter.channels}

    @property
    def paused(self) -> bool:
        return any(self.running.get(ch) is not False for ch in self.adapter.channels)

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "mode": None,
            "source": "unavailable",
            "reason": "此协议尚无已验证的模式、运行状态和波形参数读回；显示命令记录，当前物理输出未确认。",
            "channels": {
                str(ch): {
                    "mode": None,
                    "running": None,
                    "parameters": {},
                    "source": "unavailable",
                    "commanded_running": self.running.get(ch),
                    "last_command": deepcopy(self.journal.get("channels", {}).get(str(ch), {})),
                }
                for ch in self.adapter.channels
            },
            "polling_paused": self.paused,
            "restore_available": bool(self.journal.get("restore_voltage")),
            "last_mode": self.journal.get("last_mode"),
            "last_mode_at": self.journal.get("last_mode_at", ""),
            "last_result": deepcopy(self.journal.get("last_result", {})),
        }

    def _persist(self) -> None:
        self.journal["updated_at"] = utc_now()
        self.save(deepcopy(self.journal))

    def _begin(self, operation: str, channel: int, parameters: dict) -> None:
        self.journal["last_result"] = {
            "operation": operation,
            "channel": channel,
            "parameters": deepcopy(parameters),
            "outcome": "pending",
            "timestamp": utc_now(),
        }
        if operation != "normal_mode":
            record = self.journal["channels"].setdefault(str(channel), {})
            record["last_result"] = deepcopy(self.journal["last_result"])
        # Failure here prevents any device write.
        self._persist()

    def _finish(self, result: ApplyResult) -> ApplyResult:
        channel = self.journal.get("last_result", {}).get("channel", 1)
        self.journal["last_result"] = {**jsonable(result), "channel": channel}
        if result.outcome == "success":
            self.journal["last_success"] = deepcopy(self.journal["last_result"])
        if result.operation != "normal_mode":
            record = self.journal["channels"].setdefault(str(channel), {})
            record["last_result"] = deepcopy(self.journal["last_result"])
            if result.outcome == "success":
                record["last_success"] = deepcopy(record["last_result"])
        self._persist()
        return result

    def _unknown(self, operation: str, parameters: dict, error: Exception, steps: int) -> ApplyResult:
        result = ApplyResult(
            "uncertain",
            operation,
            parameters,
            completed_steps=steps,
            error=f"命令结果未确认；未自动重发，未声称已恢复：{error}",
        )
        channel = self.journal.get("last_result", {}).get("channel", 1)
        self.journal["last_result"] = {**jsonable(result), "channel": channel}
        if operation != "normal_mode":
            record = self.journal["channels"].setdefault(str(channel), {})
            record["last_result"] = deepcopy(self.journal["last_result"])
        try:
            self._persist()
        except Exception:
            # The pre-write pending record remains durable if the final save fails.
            pass
        return result

    def start(self, channel: int, parameters: dict) -> ApplyResult:
        validate_waveform(parameters)
        if self.running.get(channel) is not False:
            raise ValidationError("当前通道可能仍有波形输出，请先停止；首次使用可点击“停止波形并进入常规”。")
        if not self.journal.get("restore_voltage"):
            if self.paused:
                raise ValidationError("保存直流恢复值前，请先停止所有通道的波形。")
            voltages = self.adapter._read_voltages()
            if any(not math.isfinite(v) or not -0.01 <= v <= 120.01 for v in voltages):
                raise ValidationError("当前直流读回超出可恢复范围，未发送波形。")
            self.journal["restore_voltage"] = {
                str(ch): min(120.0, max(0.0, value))
                for ch, value in zip(self.adapter.channels, voltages, strict=True)
            }
            self.journal["restore_original_readback"] = dict(
                zip(map(str, self.adapter.channels), voltages, strict=True)
            )
        self._begin("standard_waveform", channel, parameters)
        self.running[channel] = None
        self.owned = True
        try:
            self.adapter._write_frame(build_standard_waveform(self.adapter.address, channel - 1, parameters))
            self.running[channel] = True
            self.journal["channels"][str(channel)] = {
                **self.journal["channels"].get(str(channel), {}),
                "parameters": deepcopy(parameters),
                "commanded_running": True,
                "source": "command_record",
                "timestamp": utc_now(),
            }
            self.journal.update(last_mode="waveform", last_mode_at=utc_now())
            return self._finish(
                ApplyResult(
                    "success",
                    "standard_waveform",
                    parameters,
                    completed_steps=1,
                    adaptation={"source": "command_record", "acknowledged": False},
                )
            )
        except Exception as exc:
            self.running[channel] = None
            return self._unknown("standard_waveform", parameters, exc, 1)

    def stop(self, channel: int) -> ApplyResult:
        self._begin("waveform_stop", channel, {})
        self.running[channel] = None
        try:
            self.adapter._write_frame(build_waveform_stop(self.adapter.address, channel - 1))
            self.running[channel] = False
            old = self.journal["channels"].get(str(channel), {})
            self.journal["channels"][str(channel)] = {
                **old,
                "commanded_running": False,
                "source": "command_record",
                "timestamp": utc_now(),
            }
            return self._finish(
                ApplyResult(
                    "success",
                    "waveform_stop",
                    {},
                    completed_steps=1,
                    adaptation={"source": "command_record", "acknowledged": False},
                )
            )
        except Exception as exc:
            self.running[channel] = None
            return self._unknown("waveform_stop", {}, exc, 1)

    def normal(self) -> ApplyResult:
        from lab_control.domain.instruments import DeviceCommand

        backup = self.journal.get("restore_voltage", {})
        if backup and (
            not isinstance(backup, dict)
            or set(backup) != set(map(str, self.adapter.channels))
            or any(
                type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 120
                for v in backup.values()
            )
        ):
            raise ValidationError("直流恢复快照无效；可逐路停止，不能自动恢复。")
        self._begin("normal_mode", 1, {})
        steps = 0
        self.normalizing = True
        final: dict[str, float] = {}
        try:
            for channel in self.adapter.channels:
                if self.running.get(channel) is not False:
                    self.running[channel] = None
                    steps += 1
                    self.adapter._write_frame(build_waveform_stop(self.adapter.address, channel - 1))
                    self.running[channel] = False
            for channel, value in backup.items():
                result = self.adapter.apply(DeviceCommand("voltage", {"value": value}, int(channel), True))
                steps += result.completed_steps
                if result.outcome != "success":
                    raise DeviceError(f"CH {channel} 直流恢复失败：{result.error}", "restoration_failed")
                final[channel] = result.readback
            # Independent final read, not a cached write result.
            final = dict(zip(map(str, self.adapter.channels), self.adapter._read_voltages(), strict=True))
            if any(abs(final[ch] - value) > 0.01 for ch, value in backup.items()):
                raise DeviceError("最终读回与恢复快照不符", "restoration_failed")
            self.journal["restoration"] = {
                "original": deepcopy(backup),
                "final": final,
                "timestamp": utc_now(),
            }
            self.journal.pop("restore_voltage", None)
            self.journal.update(last_mode="normal", last_mode_at=utc_now())
            for channel in self.adapter.channels:
                old = self.journal["channels"].get(str(channel), {})
                self.journal["channels"][str(channel)] = {**old, "commanded_running": False}
            result = self._finish(
                ApplyResult(
                    "success",
                    "normal_mode",
                    {},
                    final,
                    steps,
                    adaptation={"restored": bool(backup), "source": "instrument_voltage_only"},
                )
            )
            self.owned = False
            return result
        except Exception as exc:
            # Keep the complete original snapshot, even after partial restoration.
            self.journal["restore_voltage"] = deepcopy(backup)
            self.journal["restoration"] = {"final": final, "error": str(exc), "timestamp": utc_now()}
            self.running = {ch: None for ch in self.adapter.channels}
            return self._unknown("normal_mode", {}, exc, steps)
        finally:
            self.normalizing = False

    def release(self) -> None:
        if self.owned:
            result = self.normal()
            if result.outcome != "success":
                raise DeviceError(result.error, "restoration_failed")
