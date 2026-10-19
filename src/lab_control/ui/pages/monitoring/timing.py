"""Shared cadence controls: recording saves every acquired frame."""

from PySide6.QtWidgets import QDoubleSpinBox, QHBoxLayout, QLabel, QWidget

from lab_control.application.acquisition_pipeline import validate_cadence
from lab_control.application.catalog import instrument_category
from lab_control.domain.instruments import ValidationError
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox

POWER_INTERVALS = (1, 5, 10, 60)


def monitoring_intervals(controller, device_id: str, mode: str) -> tuple[float, None]:
    options = monitoring_options(controller, device_id, mode)
    return options["interval"], None


def monitoring_options(controller, device_id: str, mode: str) -> dict:
    category = instrument_category(controller.records.get(device_id, {}))
    reset_on_launch = category in {"spectrum_analyzer", "oscilloscope", "power_supply"}
    # A spectrum scan is a complete instrument sweep. Ten seconds is the
    # conservative first-run cadence; a saved per-device value still wins.
    default_interval = 10.0 if category == "optical_spectrometer" else 1.0 if category == "power_supply" else 0.5
    default_cadence = "interval" if reset_on_launch or category == "optical_spectrometer" else "fastest"
    key = f"monitor-timing/{mode}/{device_id}"
    preferences = controller.runtime.monitoring_preferences
    if key not in preferences:
        preferences[key] = (
            {} if reset_on_launch or not device_id else controller.runtime.repository.load_workspace(key, {})
        )
    stored = preferences[key]
    task = controller.runtime.monitoring.snapshots().get(device_id, {})
    values = task if task.get("running") else stored
    try:
        raw = float(values.get("interval", default_interval))
        interval = 0.5 if raw < 0.5 else raw
        cadence = values.get("cadence", "interval" if "interval" in values else default_cadence)
        validate_cadence(interval, cadence)
    except (AttributeError, TypeError, ValueError, ValidationError):
        interval, cadence = default_interval, default_cadence
    if category == "power_supply":
        interval = interval if interval in POWER_INTERVALS else 1.0
        cadence = "interval"
    # Old independent save/CSV preferences never throttle the new UI path.
    return dict(interval=interval, record_interval=None, cadence=cadence, csv_interval=5)


def save_monitoring_interval(
    controller,
    device_id: str,
    mode: str,
    interval: float,
    *,
    cadence: str | None = None,
    notify: bool = True,
) -> float:
    """Persist one device's interval and update an active scheduler.

    The overview and device pages intentionally share this small write path.
    It keeps the existing timing options (cadence and CSV settings), while
    changing only the interval requested by the user.  ``set_intervals`` is a
    local scheduler operation, so calling it while no task is active is safe
    and does not contact the instrument.
    """
    if not device_id:
        return float(interval)
    interval = float(interval)
    options = monitoring_options(controller, device_id, mode)
    category = instrument_category(controller.records.get(device_id, {}))
    if category == "power_supply":
        interval = min(POWER_INTERVALS, key=lambda option: abs(option - interval))
    options["interval"] = interval
    options["record_interval"] = None
    options["cadence"] = (
        "interval"
        if category == "power_supply"
        else cadence if cadence is not None else options.get("cadence", "interval")
    )
    controller.runtime.monitoring.set_intervals(device_id, **options)
    key = f"monitor-timing/{mode}/{device_id}"
    controller.runtime.monitoring_preferences[key] = dict(options)
    controller.runtime.repository.save_workspace(key, options)
    if notify and hasattr(controller, "monitoring_interval_changed"):
        controller.monitoring_interval_changed.emit(device_id, interval)
    return interval


class MonitoringTiming(QWidget):
    def __init__(self, controller, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.device_id, self.mode = "", ""
        self.power = False
        self.fixed_interval = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.cadence = WorkspaceComboBox(self)
        self.cadence.addItem("最快采集", "fastest")
        self.cadence.addItem("自定义间隔", "interval")
        self.cadence.setToolTip("最快：上一帧收完即安排下一帧。开启记录后，每个有效采集帧都保存。")
        label = QLabel("采集节奏", self)
        self.cadence_label = label
        label.setObjectName("mutedText")
        label.setBuddy(self.cadence)
        layout.addWidget(label)
        layout.addWidget(self.cadence)
        self.cadence.currentIndexChanged.connect(self.change_cadence)
        self.interval = QDoubleSpinBox(self)
        self.interval.setRange(0.5, 60)
        self.interval.setDecimals(2)
        self.interval.setSingleStep(0.05)
        self.interval.setValue(0.5)
        self.interval.setSuffix(" s")
        self.interval.setKeyboardTracking(False)
        self.interval.setMinimumWidth(100)
        self.interval.setMaximumWidth(132)
        self.interval.setToolTip("相邻采集请求的目标开始间隔，范围 0.5～60 秒；不改变仪器扫描时间。")
        # Spinbox edits are the device-page equivalent of changing an
        # overview card.  Commit on every accepted value so the other view
        # follows without requiring a separate save action.
        self.interval.valueChanged.connect(self._interval_value_changed)
        label = QLabel("采集间隔", self)
        self.interval_label = label
        label.setObjectName("mutedText")
        label.setBuddy(self.interval)
        layout.addWidget(label)
        layout.addWidget(self.interval)
        self.presets = WorkspaceComboBox(self)
        for seconds in POWER_INTERVALS:
            self.presets.addItem(f"{seconds} s", seconds)
        self.presets.setFixedWidth(90)
        self.presets.setToolTip("相邻采集请求的目标开始间隔；记录时逐帧保存，慢设备不补发。")
        self.presets.currentIndexChanged.connect(self.commit)
        self.presets.hide()
        layout.addWidget(self.presets)
        layout.addStretch(1)
        self.interval.setEnabled(False)
        self.actual = QLabel("实际 — s/帧", self)
        self.actual.setObjectName("rangeHint")
        layout.addWidget(self.actual)
        if hasattr(controller, "monitoring_interval_changed"):
            controller.monitoring_interval_changed.connect(self._external_interval_changed)

    def _external_interval_changed(self, device_id: str, interval: float) -> None:
        if device_id != self.device_id:
            return
        self.interval.blockSignals(True)
        self.interval.setValue(float(interval))
        self.interval.blockSignals(False)
        self.presets.blockSignals(True)
        self.presets.setCurrentIndex(max(0, self.presets.findData(float(interval))))
        self.presets.blockSignals(False)

    def _interval_value_changed(self, _value: float) -> None:
        if self.device_id:
            self.commit()

    def set_fixed_interval(self, fixed: bool = True) -> None:
        """Use the shared 0.5–60 second interval control without a cadence selector."""
        self.fixed_interval = fixed
        self._update_visibility()

    def _update_visibility(self) -> None:
        self.cadence_label.setVisible(not self.power and not self.fixed_interval)
        self.cadence.setVisible(not self.power and not self.fixed_interval)
        self.interval_label.setVisible(not self.power or self.fixed_interval)
        self.interval.setVisible(not self.power or self.fixed_interval)
        self.presets.setVisible(self.power and not self.fixed_interval)
        self.interval.setEnabled(self.fixed_interval or self.cadence.currentData() == "interval")

    def options(self) -> dict:
        return dict(
            interval=float(self.presets.currentData())
            if self.power and not self.fixed_interval
            else self.interval.value(),
            record_interval=None,
            cadence="interval" if self.power or self.fixed_interval else self.cadence.currentData(),
            csv_interval=5,
        )

    def change_cadence(self) -> None:
        self.interval.setEnabled(self.fixed_interval or self.cadence.currentData() == "interval")
        self.commit()

    @property
    def key(self) -> str:
        return f"monitor-timing/{self.mode}/{self.device_id}"

    def bind(self, device_id: str, mode: str) -> None:
        if (device_id, mode) == (self.device_id, self.mode):
            return
        self.device_id, self.mode = device_id, mode
        self.power = instrument_category(self.controller.records.get(device_id, {})) == "power_supply"
        self._update_visibility()
        self.setEnabled(bool(device_id))
        options = monitoring_options(self.controller, device_id, mode)
        self.cadence.blockSignals(True)
        self.cadence.setCurrentIndex(self.cadence.findData(options["cadence"]))
        self.cadence.blockSignals(False)
        self.interval.setEnabled(self.fixed_interval or options["cadence"] == "interval")
        self.interval.blockSignals(True)
        self.interval.setValue(options["interval"])
        self.interval.blockSignals(False)
        self.presets.blockSignals(True)
        self.presets.setCurrentIndex(max(0, self.presets.findData(options["interval"])))
        self.presets.blockSignals(False)
        self.refresh(self.controller.runtime.monitoring.snapshots().get(device_id, {}))

    def commit(self) -> None:
        if not self.device_id:
            return
        options = self.options()
        try:
            save_monitoring_interval(
                self.controller,
                self.device_id,
                self.mode,
                options["interval"],
                cadence=options["cadence"],
            )
        except Exception as exc:
            self.controller.message.emit(str(exc))

    def refresh(self, task: dict) -> None:
        if (
            self.device_id
            and not self.interval.hasFocus()
            and not self.cadence.view().isVisible()
            and not self.presets.view().isVisible()
        ):
            options = monitoring_options(self.controller, self.device_id, self.mode)
            self.cadence.blockSignals(True)
            self.interval.blockSignals(True)
            self.cadence.setCurrentIndex(self.cadence.findData(options["cadence"]))
            self.interval.setValue(options["interval"])
            self.interval.setEnabled(self.fixed_interval or options["cadence"] == "interval")
            self.presets.blockSignals(True)
            self.presets.setCurrentIndex(max(0, self.presets.findData(options["interval"])))
            self.presets.blockSignals(False)
            self.cadence.blockSignals(False)
            self.interval.blockSignals(False)
        actual = task.get("frame_interval")
        self.actual.setText(f"实际 {actual:.2f} s/帧" if actual is not None else "实际 — s/帧")
