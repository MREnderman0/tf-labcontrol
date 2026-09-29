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
    default_interval = 1.0 if category == "power_supply" else 0.5
    default_cadence = "interval" if reset_on_launch else "fastest"
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


class MonitoringTiming(QWidget):
    def __init__(self, controller, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.device_id, self.mode = "", ""
        self.power = False
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
        self.interval.editingFinished.connect(self.commit)
        label = QLabel("采集间隔", self)
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

    def options(self) -> dict:
        return dict(
            interval=float(self.presets.currentData()) if self.power else self.interval.value(),
            record_interval=None,
            cadence="interval" if self.power else self.cadence.currentData(),
            csv_interval=5,
        )

    def change_cadence(self) -> None:
        self.interval.setEnabled(self.cadence.currentData() == "interval")
        self.commit()

    @property
    def key(self) -> str:
        return f"monitor-timing/{self.mode}/{self.device_id}"

    def bind(self, device_id: str, mode: str) -> None:
        if (device_id, mode) == (self.device_id, self.mode):
            return
        self.device_id, self.mode = device_id, mode
        self.power = instrument_category(self.controller.records.get(device_id, {})) == "power_supply"
        self.cadence_label.setVisible(not self.power)
        self.cadence.setVisible(not self.power)
        self.interval.setVisible(not self.power)
        self.presets.setVisible(self.power)
        self.setEnabled(bool(device_id))
        options = monitoring_options(self.controller, device_id, mode)
        self.cadence.blockSignals(True)
        self.cadence.setCurrentIndex(self.cadence.findData(options["cadence"]))
        self.cadence.blockSignals(False)
        self.interval.setEnabled(options["cadence"] == "interval")
        self.interval.setValue(options["interval"])
        self.presets.blockSignals(True)
        self.presets.setCurrentIndex(max(0, self.presets.findData(options["interval"])))
        self.presets.blockSignals(False)
        self.refresh(self.controller.runtime.monitoring.snapshots().get(device_id, {}))

    def commit(self) -> None:
        if not self.device_id:
            return
        options = self.options()
        try:
            self.controller.runtime.monitoring.set_intervals(self.device_id, **options)
            self.controller.runtime.monitoring_preferences[self.key] = dict(options)
            self.controller.runtime.repository.save_workspace(self.key, options)
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
            self.interval.setEnabled(options["cadence"] == "interval")
            self.presets.blockSignals(True)
            self.presets.setCurrentIndex(max(0, self.presets.findData(options["interval"])))
            self.presets.blockSignals(False)
            self.cadence.blockSignals(False)
            self.interval.blockSignals(False)
        actual = task.get("frame_interval")
        self.actual.setText(f"实际 {actual:.2f} s/帧" if actual is not None else "实际 — s/帧")
