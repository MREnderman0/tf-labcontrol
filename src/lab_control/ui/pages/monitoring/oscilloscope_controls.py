"""Compact capability-driven scope sliders; one serial control group per gesture."""

from __future__ import annotations

import math
import re
from typing import Any

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QIntValidator, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from lab_control.domain.instruments import DeviceCommand, DeviceError, Parameter
from lab_control.domain.oscilloscope import COLORS, CONTROL_NAMES, coarse_steps, engineering
from lab_control.domain.readback import boolean_value, normalized_value, usable_reading
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox


def gear_icon() -> QIcon:
    pixmap = QPixmap(48, 48)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(2, 2)
    painter.translate(12, 12)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#b7c5bf"))
    for _ in range(8):
        painter.drawRoundedRect(QRectF(-1.8, -9, 3.6, 4), 0.7, 0.7)
        painter.rotate(45)
    painter.drawEllipse(QRectF(-6.5, -6.5, 13, 13))
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
    painter.drawEllipse(QRectF(-3, -3, 6, 6))
    painter.end()
    return QIcon(pixmap)


class GestureSlider(QSlider):
    key_finished = Signal()
    fine_step = Signal(int)
    linear = False
    key_active = False

    def keyPressEvent(self, event) -> None:  # noqa: N802
        self.key_active = True
        directions = {
            Qt.Key.Key_Left: -1,
            Qt.Key.Key_Down: -1,
            Qt.Key.Key_Right: 1,
            Qt.Key.Key_Up: 1,
            Qt.Key.Key_PageDown: -100,
            Qt.Key.Key_PageUp: 100,
        }
        if self.linear and event.key() in directions:
            self.fine_step.emit(directions[event.key()])
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:  # noqa: N802
        super().keyReleaseEvent(event)
        if not event.isAutoRepeat():
            self.key_active = False
            self.key_finished.emit()


class ScopeSlider(QWidget):
    committed = Signal(float)

    def __init__(self, title: str, unit: str, *, coarse=False, parent=None) -> None:
        super().__init__(parent)
        self.unit, self.coarse = unit, coarse
        self.steps: tuple[float, ...] = ()
        self.low, self.high, self.step = 0.0, 1.0, 0.01
        self.actual: float | None = None
        self.signature: tuple | None = None
        self.dirty = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        layout = QHBoxLayout()
        outer.addLayout(layout)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        self.setMinimumHeight(40)
        self.title = QLabel(title)
        self.title.setObjectName("scopeControlLabel")
        self.title.setFixedWidth(28)
        self.value_label = QLabel("—")
        self.value_label.setObjectName("scopeControlValue")
        self.value_label.setMinimumWidth(90)
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.title)
        self.slider = GestureSlider(Qt.Orientation.Horizontal)
        self.slider.linear = not coarse
        self.target: float | None = None
        self.slider.setAccessibleName(title)
        self.slider.setFixedHeight(18)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.value_label)
        endpoints = QHBoxLayout()
        self.minimum_label, self.maximum_label = QLabel(), QLabel()
        for label in (self.minimum_label, self.maximum_label):
            label.setObjectName("rangeHint")
        endpoints.addWidget(self.minimum_label)
        endpoints.addStretch()
        endpoints.addWidget(self.maximum_label)
        outer.addLayout(endpoints)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(180)
        self.timer.timeout.connect(self.commit)
        self.slider.valueChanged.connect(self.preview)
        self.slider.sliderPressed.connect(self.begin_gesture)
        self.slider.sliderReleased.connect(self.commit)
        self.slider.key_finished.connect(self.timer.start)
        self.slider.fine_step.connect(self.keyboard_step)

    def begin_gesture(self) -> None:
        self.timer.stop()
        # The existing instrument value may now lie beyond a changed endpoint.
        # An explicit press/release can select that endpoint even without travel;
        # simply refreshing the range must never write it to the instrument.
        if self.actual is not None and not math.isclose(self.value(), self.actual, rel_tol=1e-9, abs_tol=0):
            self.dirty = True
            self.value_label.setText("→ " + self.text(self.value()))

    def value(self) -> float:
        if self.coarse:
            return self.steps[self.slider.value()]
        if self.target is not None:
            return self.target
        if self.slider.value() == self.slider.minimum():
            return self.low
        if self.slider.value() == self.slider.maximum():
            return self.high
        raw = self.low + self.slider.value() / max(1, self.slider.maximum()) * (self.high - self.low)
        raw = round(raw / self.step) * self.step
        return min(self.high, max(self.low, float(f"{raw:.12g}")))

    def keyboard_step(self, direction: int) -> None:
        if self.actual is None:
            return
        initial = self.value() if self.dirty else self.actual
        self.target = min(self.high, max(self.low, float(f"{initial + direction * self.step:.12g}")))
        self.slider.blockSignals(True)
        self.slider.setValue(round((self.target - self.low) / (self.high - self.low) * self.slider.maximum()))
        self.slider.blockSignals(False)
        self.dirty = True
        self.value_label.setText("→ " + self.text(self.target))

    def preview(self) -> None:
        self.target = None
        self.dirty = True
        self.value_label.setText("→ " + self.text(self.value()))
        if not self.slider.isSliderDown() and not self.slider.key_active:
            self.timer.start()

    def text(self, value: float) -> str:
        return engineering(value, self.unit, custom_scale=self.coarse) + ("/格" if self.coarse else "")

    def commit(self) -> None:
        self.timer.stop()
        if not self.dirty:
            return
        self.dirty = False
        if self.isEnabled() and self.actual is not None:
            self.committed.emit(self.value())
        elif self.actual is not None:
            self.signature = None
            self.value_label.setText(self.text(self.actual))

    def cancel(self) -> None:
        self.timer.stop()
        self.dirty = False
        self.slider.key_active = False
        self.signature = None
        self.target = None

    def configure(self, actual: float, spec: Parameter, division: float) -> bool:
        if self.dirty or self.slider.isSliderDown():
            return True
        if spec.minimum is None or spec.maximum is None or spec.maximum <= spec.minimum:
            return False
        signature = (actual, spec, division)
        if self.signature == signature:
            return True
        self.actual, self.signature = actual, signature
        self.target = None
        self.low, self.high = spec.minimum, spec.maximum
        self.slider.blockSignals(True)
        if self.coarse:
            self.steps = coarse_steps(self.low, self.high)
            if not self.steps:
                self.slider.blockSignals(False)
                return False
            self.slider.setRange(0, len(self.steps) - 1)
            self.slider.setValue(min(range(len(self.steps)), key=lambda i: abs(self.steps[i] - actual)))
            self.slider.setPageStep(1)
        else:
            self.step = spec.resolution or max(abs(division) / 100, 1e-15)
            ticks = min(2_000_000_000, max(1, math.ceil((self.high - self.low) / self.step)))
            self.slider.setRange(0, ticks)
            self.slider.setValue(round((actual - self.low) / (self.high - self.low) * ticks))
            self.slider.setPageStep(100)
        self.slider.blockSignals(False)
        self.value_label.setText(self.text(actual))
        self.value_label.setToolTip(f"实际读回：{actual:.12g} {self.unit}")
        self.minimum_label.setText(engineering(self.steps[0] if self.coarse else self.low, self.unit))
        self.maximum_label.setText(engineering(self.steps[-1] if self.coarse else self.high, self.unit))
        self.slider.setToolTip(
            f"合法范围：{self.low:.12g} ～ {self.high:.12g} {self.unit} · 松手应用"
            + (f" · 键盘步进 {self.step:.12g} {self.unit}" if not self.coarse else "")
        )
        return True


class OscilloscopeControls(QFrame):
    busy_changed = Signal(bool)
    channels_changed = Signal()

    def __init__(self, controller, device_id: str, resume, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("scopeControls")
        self.controller, self.device_id, self.resume = controller, device_id, resume
        self.running = False
        self.session_id = ""
        self.values: dict[tuple[int, str], Any] = {}
        self.sliders: dict[tuple[int, str], ScopeSlider] = {}
        self.checks: dict[int, QCheckBox] = {}
        self.pending: list[DeviceCommand] = []
        self.read_pending: list[tuple[int, tuple[str, ...]]] = []
        self.restart = False
        self.channel_change = False
        self.last_context: tuple | None = None
        self.point_limit_before: float | None = None
        self.point_clamp_attempted = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.health = QLabel("同步中")
        self.health.setObjectName("spectrumHealth")
        layout.addWidget(self.health)
        self.scroller = QScrollArea()
        self.scroller.setWidgetResizable(True)
        self.scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        stack = QVBoxLayout(body)
        stack.setContentsMargins(0, 0, 6, 0)
        stack.setSpacing(7)

        def section(title):
            frame = QFrame()
            frame.setObjectName("scopeSection")
            box = QVBoxLayout(frame)
            box.setContentsMargins(10, 5, 10, 5)
            box.setSpacing(2)
            if title:
                label = QLabel(title)
                label.setObjectName("spectrumChartTitle")
                box.addWidget(label)
            stack.addWidget(frame)
            return box

        horizontal = section("水平")
        self.add_slider(horizontal, 1, "timebase", "时基", "s", coarse=True)
        self.add_slider(horizontal, 1, "time_position", "偏移", "s")
        point_row = QHBoxLayout()
        point_row.addWidget(QLabel("采集点数"))
        self.points = QLineEdit()
        self.points.setAccessibleName("采集点数")
        self.points.setValidator(QIntValidator(1, 2_147_483_647, self.points))
        self.points.setMaximumWidth(100)
        self.points.editingFinished.connect(self.set_points)
        point_row.addWidget(self.points)
        self.points_range = QLabel("等待读回")
        self.points_range.setObjectName("rangeHint")
        self.points_range.setWordWrap(True)
        point_row.addWidget(self.points_range, 1)
        horizontal.addLayout(point_row)
        trigger = section("")
        row = QHBoxLayout()
        self.trigger_label = QLabel("触发 · 等待读回")
        self.trigger_label.setObjectName("spectrumChartTitle")
        row.addWidget(self.trigger_label, 1)
        self.gear = QToolButton()
        self.gear.setIcon(gear_icon())
        self.gear.setIconSize(QSize(20, 20))
        self.gear.setFixedSize(26, 24)
        self.gear.setAccessibleName("触发设置")
        self.gear.setToolTip("触发源、边沿和模式")
        self.gear.clicked.connect(self.trigger_dialog)
        row.addWidget(self.gear)
        trigger.addLayout(row)
        self.trigger_slider = ScopeSlider("电平", "V")
        self.trigger_slider.committed.connect(self.set_trigger_level)
        trigger.addWidget(self.trigger_slider)
        for ch in controller.capabilities(device_id).channels:
            box = section("")
            check = QCheckBox(f"CH {ch}")
            check.setStyleSheet(f"color: {COLORS[(ch - 1) % 4]}; font-weight: 600;")
            check.clicked.connect(lambda enabled, c=ch: self.set_channel(c, enabled))
            box.addWidget(check)
            self.checks[ch] = check
            self.add_slider(box, ch, "scale", "档位", "V", coarse=True)
            self.add_slider(box, ch, "offset", "偏移", "V")
        stack.addStretch()
        self.scroller.setWidget(body)
        layout.addWidget(self.scroller, 1)
        self.status = QLabel("拖动预览目标值 · 松手后写入仪器")
        self.status.setObjectName("rangeHint")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        controller.refreshed.connect(self.refresh)
        self.refresh()

    def add_slider(self, layout, channel, name, title, unit, *, coarse=False) -> None:
        field = ScopeSlider(title, unit, coarse=coarse)
        self.sliders[channel, name] = field
        field.committed.connect(lambda value: self.submit([DeviceCommand(name, {"value": value}, channel)]))
        layout.addWidget(field)

    def capabilities(self, channel=1):
        return {c.name: c for c in self.controller.capabilities(self.device_id, channel).operations}

    def keys(self) -> tuple[tuple[int, str], ...]:
        return tuple(
            dict.fromkeys(
                (ch if cap.channel_scoped else 1, cap.name)
                for ch in self.checks
                for cap in self.capabilities(ch).values()
                if cap.readable
                and cap.available
                and cap.name in (*CONTROL_NAMES, "record_length", "coupling", "probe_attenuation")
            )
        )

    def channels(self) -> tuple[int, ...]:
        return tuple(ch for ch in self.checks if boolean_value(self.values.get((ch, "channel_enabled"))))

    def trigger_channel(self) -> int | None:
        source = str(self.values.get((1, "trigger_source"), ""))
        match = re.fullmatch(r"(?:C|CH|CHAN)([1-9])", source.upper())
        return int(match[1]) if match and int(match[1]) in self.checks else None

    def refresh(self) -> None:
        state = self.controller.states.get(self.device_id, {})
        context = (state.get("sessionId"), state.get("configurationVersion"))
        if context != self.last_context:
            self.values.clear()
            for field in (*self.sliders.values(), self.trigger_slider):
                field.cancel()
            self.last_context = context
        if not self.running:
            for key, frame in self.controller.runtime.sessions.readback_frames(self.device_id).items():
                cap = self.capabilities(key[0]).get(key[1])
                if (
                    (frame.session_id, frame.configuration_version) == context
                    and cap
                    and usable_reading(frame.reading, {} if key[1] == "trigger_source" else cap.parameters)
                ):
                    value = frame.reading.value
                    self.values[key] = value.get("value") if isinstance(value, dict) else value
        available = (
            state.get("connectionState") in {"connected", "busy"}
            and not state.get("controlsLocked")
            and not state.get("controlGroup")
            and not state.get("capabilityStale", True)
            and not self.running
            and self.device_id not in self.controller.group_devices
        )
        point_cap = self.capabilities().get("record_length")
        point_value = self.values.get((1, "record_length"))
        if point_value is not None and not self.points.hasFocus():
            self.points.setText(str(int(point_value)))
        self.points.setEnabled(
            bool(available and point_cap and point_cap.writable and point_value is not None)
        )
        if point_cap and "value" in point_cap.parameters:
            point_spec = point_cap.parameters["value"]
            if point_spec.minimum is not None and point_spec.maximum is not None:
                self.points_range.setText(f"{point_spec.minimum:,.0f}～{point_spec.maximum:,.0f}\n含软件限制")
                if point_spec.choices:
                    self.points_range.setText(
                        self.points_range.text()
                        + "\n档位："
                        + " / ".join(engineering(float(v), "").replace(" ", "") for v in point_spec.choices)
                    )
                self.points.setToolTip(
                    point_cap.reason
                    or "仪器最大采集长度；实际有效点数以收到的波形为准。超范围原值不自动修改。"
                )
        for (ch, name), field in self.sliders.items():
            cap = self.capabilities(ch).get(name)
            actual = self.values.get((ch, name))
            division = (
                self.values.get((1, "timebase"), 1)
                if name.startswith("time")
                else self.values.get((ch, "scale"), 1)
            )
            valid = bool(cap and cap.available and actual is not None and "value" in cap.parameters)
            if valid and actual is not None:
                valid = field.configure(float(actual), cap.parameters["value"], float(division))
            enabled = name.startswith("time") or ch in self.channels()
            field.setEnabled(bool(available and valid and cap and cap.writable and enabled))
        for ch, check in self.checks.items():
            cap = self.capabilities(ch).get("channel_enabled")
            value = boolean_value(self.values.get((ch, "channel_enabled")))
            if value is not None:
                check.setChecked(value)
            check.setEnabled(bool(available and value is not None and cap and cap.writable and cap.available))
        trigger_ch = self.trigger_channel()
        cap = self.capabilities(trigger_ch or 1).get("trigger_level")
        level_key = (trigger_ch or 1 if cap and cap.channel_scoped else 1, "trigger_level")
        level = self.values.get(level_key)
        valid = bool(trigger_ch and cap and cap.available and cap.writable and level is not None)
        if valid and level is not None:
            valid = self.trigger_slider.configure(
                float(level), cap.parameters["value"], self.values.get((trigger_ch or 1, "scale"), 1)
            )
        self.trigger_slider.setEnabled(bool(available and valid))
        self.trigger_slider.setToolTip(cap.reason if cap else "当前型号未提供触发电平")
        source = self.values.get((1, "trigger_source"), "—")
        mode = self.values.get((1, "trigger_mode"), "—")
        self.trigger_label.setText(f"触发 · {source} · {mode}")
        self.gear.setEnabled(bool(available and self.values))
        self.health.setText(
            "设置中 · 完成后自动续采"
            if self.running
            else "记录收尾前参数锁定"
            if state.get("controlsLocked")
            else "参数已同步"
            if all((ch, "channel_enabled") in self.values for ch in self.checks)
            else "等待参数同步"
        )

    def set_channel(self, channel: int, enabled: bool) -> None:
        cap = self.capabilities(channel)["channel_enabled"]
        value = enabled if cap.parameters["value"].kind == "boolean" else ("ON" if enabled else "OFF")
        self.submit([DeviceCommand("channel_enabled", {"value": value}, channel)], channels=True)

    def set_points(self) -> None:
        if self.running or not self.points.isEnabled():
            return
        try:
            value = int(self.points.text())
            if value != self.values.get((1, "record_length")):
                self.capabilities()["record_length"].validate({"value": value})
                self.submit([DeviceCommand("record_length", {"value": value})])
        except (ValueError, DeviceError) as exc:
            self.status.setText(f"采集点数未写入：{exc}")

    def set_trigger_level(self, value: float) -> None:
        ch = self.trigger_channel()
        if ch:
            cap = self.capabilities(ch)["trigger_level"]
            self.submit([DeviceCommand("trigger_level", {"value": value}, ch if cap.channel_scoped else 1)])

    def submit(self, commands: list[DeviceCommand], *, channels=False) -> None:
        if self.running:
            return
        try:
            self.controller.ensure_applicable(self.device_id)
            for cmd in commands:
                cap = self.controller.capabilities(self.device_id, cmd.channel).get(cmd.operation)
                if not cap.writable:
                    raise DeviceError(cap.reason or "此设置暂不可写")
                cap.validate(cmd.parameters)
            self.controller.begin_group(self.device_id)
            point_cap = self.capabilities().get("record_length")
            self.point_limit_before = point_cap.parameters["value"].maximum if point_cap else None
            self.point_clamp_attempted = False
            self.session_id = self.controller.states[self.device_id]["sessionId"]
            self.running, self.channel_change = True, channels
            self.pending = list(commands)
            self.restart = channels and bool(
                self.controller.runtime.monitoring.snapshots().get(self.device_id, {}).get("running")
            )
            self.busy_changed.emit(True)
            self.refresh()
            if channels:
                self.controller.watch(
                    self.controller.runtime.monitoring.stop(self.device_id),
                    lambda _: self.next_command(),
                    self.failed,
                )
            elif not commands:
                self.controller.watch(
                    self.controller.runtime.sessions.submit(self.device_id, "refresh_capabilities"),
                    lambda _: self.next_command(),
                    self.failed,
                )
            else:
                self.next_command()
        except Exception as exc:
            self.failed(exc)

    def valid_session(self) -> bool:
        return any(
            s["stableDeviceId"] == self.device_id and s["sessionId"] == self.session_id
            for s in self.controller.runtime.sessions.snapshots()
        )

    def next_command(self) -> None:
        if not self.valid_session():
            self.failed(DeviceError("会话已改变，停止后续操作"))
            return
        if self.pending:
            self.controller.apply(
                self.device_id, self.pending[0], self.applied, self.failed, group_member=True
            )
            return
        self.read_pending = []
        for ch in self.checks:
            names = tuple(name for channel, name in self.keys() if channel == ch)
            self.read_pending.extend((ch, names[i : i + 4]) for i in range(0, len(names), 4))
        self.read_next()

    def applied(self, result) -> None:
        if result.outcome != "success":
            self.failed(DeviceError(result.error or result.outcome))
            return
        self.pending.pop(0)
        self.next_command()

    def read_next(self) -> None:
        if not self.valid_session():
            self.failed(DeviceError("会话已改变，停止后续读取"))
        elif self.read_pending:
            ch, names = self.read_pending.pop(0)
            self.controller.watch(
                self.controller.runtime.sessions.submit(self.device_id, "read_settings", (names, ch)),
                lambda _: self.read_next(),
                self.failed,
            )
        else:
            try:
                if not self.clamp_points_after_range_change():
                    self.finish()
            except Exception as exc:
                self.failed(exc)

    def clamp_points_after_range_change(self) -> bool:
        """Only explicit control groups may lower a value after its range shrinks."""
        cap = self.capabilities().get("record_length")
        if not cap or not cap.available or not cap.writable or self.point_limit_before is None:
            return False
        spec = cap.parameters["value"]
        maximum = spec.maximum
        if maximum is None or maximum >= self.point_limit_before:
            return False
        frame = self.controller.runtime.sessions.readback_frames(self.device_id).get((1, "record_length"))
        if (
            not frame
            or frame.session_id != self.session_id
            or not usable_reading(frame.reading, cap.parameters)
        ):
            raise DeviceError("点数范围已变化，但当前点数未可靠读回，请刷新核对")
        value = frame.reading.value
        current = normalized_value(value.get("value") if isinstance(value, dict) else value, spec)
        if current <= maximum:
            return False
        if self.point_clamp_attempted:
            raise DeviceError("自动降低点数后仍超出新范围，已停止后续设置")
        target = math.floor(maximum)
        if spec.choices:
            target = max(int(float(v)) for v in spec.choices if float(v) <= maximum)
        cap.validate({"value": target})
        self.point_clamp_attempted = True
        self.pending = [DeviceCommand("record_length", {"value": target})]
        self.next_command()
        return True

    def failed(self, exc) -> None:
        self.status.setText(f"未完成：{exc} · 未重发设置，请刷新核对")
        if self.running:
            self.controller.watch(self.controller.runtime.monitoring.stop(self.device_id))
            self.finish(success=False)
        else:
            self.refresh()

    def finish(self, *, success=True) -> None:
        restart = self.restart and success
        self.running, self.restart = False, False
        self.pending.clear()
        self.controller.end_group(self.device_id)
        self.busy_changed.emit(False)
        # Refresh reads are already cached by the worker; polling publishes the version.
        self.controller.states.update(
            {s["stableDeviceId"]: s for s in self.controller.runtime.sessions.snapshots()}
        )
        self.refresh()
        if success:
            if (1, "record_length") in self.values:
                self.points.setText(str(int(self.values[1, "record_length"])))
            self.status.setText(
                "已核对实际参数"
                + (" · 单次模式：等待新波形" if self.values.get((1, "trigger_mode")) == "SINGLE" else "")
            )
            if self.channel_change:
                self.channels_changed.emit()
            if restart and self.channels():
                self.resume()

    def refresh_parameters(self) -> None:
        self.submit([])

    def trigger_dialog(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("触发设置")
        dialog.setObjectName("sourceSettingsDialog")
        dialog.setMinimumWidth(320)
        form = QFormLayout(dialog)
        combos = {}
        source = WorkspaceComboBox(dialog)
        caps = self.capabilities()
        source_cap = caps.get("trigger_source")
        for value in source_cap.parameters["value"].choices if source_cap else ():
            source.addItem(value, value)
        current_source = self.values.get((1, "trigger_source"))
        if current_source is not None and source.findData(current_source) < 0:
            source.addItem(str(current_source) + "（只读）", current_source)
        source.setCurrentIndex(source.findData(current_source))
        source.setEnabled(
            bool(source_cap and source_cap.available and source_cap.writable and current_source is not None)
        )
        form.addRow("触发源", source)
        combos["trigger_source"] = source
        for name, title in (("trigger_slope", "边沿"), ("trigger_mode", "模式")):
            combo = WorkspaceComboBox(dialog)
            form.addRow(title, combo)
            combos[name] = combo

        def update():
            match = re.fullmatch(r"(?:C|CH|CHAN)([1-9])", str(source.currentData()))
            ch = int(match[1]) if match else None
            for name in ("trigger_slope", "trigger_mode"):
                combo = combos[name]
                cap = self.capabilities(ch or 1).get(name)
                combo.clear()
                for value in cap.parameters["value"].choices if cap else ():
                    combo.addItem(
                        {"POS": "上升沿", "NEG": "下降沿", "SINGLE": "单次等待", "STOP": "停止"}.get(
                            value, value
                        ),
                        value,
                    )
                target = (ch or 1) if cap and cap.channel_scoped else 1
                value = self.values.get((target, name))
                combo.setCurrentIndex(combo.findData(value))
                combo.setEnabled(bool(cap and cap.available and cap.writable and value is not None))

        source.currentIndexChanged.connect(update)
        update()
        notice = QLabel("仅修改本次选择的字段；不改变其他触发条件。")
        notice.setWordWrap(True)
        form.addRow(notice)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel
        )
        form.addRow(buttons)
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        context = self.last_context
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                if context != self.last_context:
                    raise DeviceError("弹窗打开期间参数已变化，请重新打开")
                selected = str(source.currentData())
                match = re.fullmatch(r"(?:C|CH|CHAN)([1-9])", selected)
                ch = int(match[1]) if match else 1
                commands = []
                for name, combo in combos.items():
                    cap = self.capabilities(ch).get(name)
                    channel = ch if cap and cap.channel_scoped else 1
                    value = combo.currentData()
                    if combo.isEnabled() and value is not None and value != self.values.get((channel, name)):
                        commands.append(DeviceCommand(name, {"value": value}, channel, confirmed=True))
                if commands:
                    self.submit(commands)
            except DeviceError as exc:
                self.status.setText(str(exc))
        dialog.deleteLater()
