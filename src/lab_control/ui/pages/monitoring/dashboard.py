from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.bulk_connection import device_label
from lab_control.application.catalog import instrument_category, mode_applicable
from lab_control.application.config import config_from_record
from lab_control.application.controls import CATEGORY_LABELS, data_sources, slot_label
from lab_control.domain.instruments import AcquisitionRequest, DeviceError
from lab_control.domain.readback import boolean_value
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.pages.monitoring.recording_state import (
    recording_phase,
    recording_save_notice,
    update_recording_frame,
)
from lab_control.ui.pages.monitoring.timing import (
    POWER_INTERVALS,
    monitoring_options,
    save_monitoring_interval,
)


def scope_channels(controller, device_id: str, channels) -> tuple[int, ...]:
    """Use current confirmed OFF values; missing reads never imply a hardware write."""
    state = controller.states.get(device_id, {})
    frames = controller.runtime.sessions.readback_frames(device_id)
    result = []
    for ch in channels:
        frame = frames.get((ch, "channel_enabled"))
        if frame and (frame.session_id, frame.configuration_version) == (
            state.get("sessionId"),
            state.get("configurationVersion"),
        ):
            if boolean_value(frame.reading.value) is False:
                continue
        result.append(ch)
    return tuple(result)


class ResponsiveGrid(QScrollArea):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.grid = QGridLayout(self.body)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(16)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setWidget(self.body)
        self.items: list[QWidget] = []
        self.columns = 0
        self.viewport().installEventFilter(self)

    def eventFilter(self, obj, event):  # noqa: N802
        if obj is self.viewport() and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self.reflow)
        return super().eventFilter(obj, event)

    def set_items(self, items: Sequence[QWidget]) -> None:
        self.items = list(items)
        self.reflow(force=True)
        QTimer.singleShot(0, self.reflow)

    def reflow(self, force: bool = False) -> None:
        columns = max(1, (self.viewport().width() + 16) // 376)
        if columns == self.columns and not force:
            return
        old_columns = self.columns
        self.columns = columns
        while self.grid.count():
            self.grid.takeAt(0)
        for index in range(max(old_columns, columns)):
            self.grid.setColumnStretch(index, 1 if index < columns else 0)
            self.grid.setColumnMinimumWidth(index, 0)
        for index, item in enumerate(self.items):
            self.grid.addWidget(item, index // columns, index % columns, Qt.AlignmentFlag.AlignTop)


class RecordingDeviceCard(QFrame):
    """One connected, recordable device in the monitoring overview."""

    changed = Signal()
    open_requested = Signal(str)

    def __init__(self, controller: WorkspaceController, spec: dict[str, Any], parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.spec = spec
        self.setObjectName("monitorCard")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        heading = QHBoxLayout()
        self.title = QPushButton()
        self.title.setObjectName("cardLink")
        self.title.clicked.connect(lambda: self.open_requested.emit(self.spec["device_id"]))
        heading.addWidget(self.title, 1)
        self.category = QLabel()
        self.category.setObjectName("eyebrow")
        heading.addWidget(self.category)
        layout.addLayout(heading)

        self.slot = QLabel()
        self.slot.setObjectName("monitorSlot")
        layout.addWidget(self.slot)
        controls = QHBoxLayout()
        self.enabled = QCheckBox("参与采样")
        self.enabled.toggled.connect(self._changed)
        controls.addWidget(self.enabled)
        controls.addStretch()
        controls.addWidget(QLabel("采样间隔"))
        self.interval = QDoubleSpinBox()
        self.interval.setRange(0.5, 60.0)
        self.interval.setSingleStep(0.5)
        self.interval.setDecimals(2)
        self.interval.setSuffix(" 秒")
        self.interval.valueChanged.connect(self._changed)
        controls.addWidget(self.interval)
        layout.addLayout(controls)

        status_row = QHBoxLayout()
        self.state = QLabel()
        self.state.setObjectName("monitorState")
        self.state.setWordWrap(False)
        status_row.addWidget(self.state)
        status_row.addStretch()
        self.details_link = QLabel('<a href="#details" style="color:#e3a252; text-decoration:none;">详情</a>')
        self.details_link.setObjectName("monitorDetailsLink")
        self.details_link.setTextFormat(Qt.TextFormat.RichText)
        self.details_link.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.details_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.details_link.linkActivated.connect(lambda _link: self.show_details())
        status_row.addWidget(self.details_link)
        layout.addLayout(status_row)

        self.details_dialog = QDialog(self)
        self.details_dialog.setObjectName("monitorDetailsDialog")
        self.details_dialog.setWindowTitle("监测详情")
        self.details_dialog.setModal(False)
        details_layout = QVBoxLayout(self.details_dialog)
        details_layout.setContentsMargins(18, 16, 18, 16)
        details_layout.setSpacing(8)
        self.identity = QLabel()
        self.identity.setObjectName("mutedText")
        self.identity.setWordWrap(True)
        details_layout.addWidget(self.identity)
        runtime_title = QLabel("采样运行信息")
        runtime_title.setObjectName("monitorDetailsHeading")
        details_layout.addWidget(runtime_title)
        self.detail_state = QLabel()
        self.detail_state.setObjectName("mutedText")
        self.detail_state.setWordWrap(True)
        details_layout.addWidget(self.detail_state)
        self.detail_path = QLabel()
        self.detail_path.setObjectName("monitorDetailsPath")
        self.detail_path.setWordWrap(True)
        details_layout.addWidget(self.detail_path)
        self.detail_frames = QLabel()
        self.detail_frames.setObjectName("monitorDetailsMeta")
        self.detail_frames.setWordWrap(True)
        details_layout.addWidget(self.detail_frames)
        details_layout.addStretch()
        self.set_spec(spec)

    def _changed(self, *_args) -> None:
        self.spec["enabled"] = self.enabled.isChecked()
        self.spec["interval"] = self.interval.value()
        self.changed.emit()

    def set_spec(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        self.title.setText(spec["title"])
        self.category.setText(spec["category_label"])
        self.identity.setText(spec["identity"])
        self.slot.setText(f"功能槽位：{spec['slot_label']}")
        self.details_dialog.setWindowTitle(f"{spec['title']} · 监测详情")
        self.setToolTip(f"本设备：{spec['title']}\n{spec['identity']}")
        self.enabled.blockSignals(True)
        self.enabled.setChecked(bool(spec.get("enabled", True)))
        self.enabled.blockSignals(False)
        self.interval.blockSignals(True)
        self.interval.setValue(float(spec.get("interval", 0.5)))
        self.interval.blockSignals(False)
        self.refresh()

    def show_details(self) -> None:
        self.details_dialog.adjustSize()
        self.details_dialog.setMinimumWidth(390)
        self.details_dialog.show()
        self.details_dialog.raise_()
        self.details_dialog.activateWindow()

    def refresh(self) -> None:
        device_id = self.spec["device_id"]
        record = self.controller.records.get(device_id)
        if not record or not self.controller.has_connected_session(device_id):
            self.hide()
            return
        self.show()
        task = self.controller.runtime.monitoring.snapshots().get(device_id, {})
        phase = recording_phase(task)
        self.setProperty("recordingPhase", phase)
        self.style().unpolish(self)
        self.style().polish(self)
        if task.get("error"):
            detail = task["error"]
            status = "异常"
        elif recording_phase(task) == "draining":
            detail = "记录收尾中 · 正在排空已接收数据"
            status = "收尾中"
        elif recording_save_notice(task):
            detail = recording_save_notice(task)
            status = "记录中" if task.get("recording") else "待处理"
        elif task.get("recording"):
            detail = "正在连续记录有效采集帧"
            status = "记录中"
        elif task.get("running"):
            detail = "监测中 · 尚未开启记录"
            status = "监测中"
        else:
            detail = "待开始采样"
            status = "待开始"
        self.state.setText(status)
        self.state.setToolTip(detail)
        self.detail_state.setText(detail)
        recording_state = task.get("recording_state", {})
        folder = recording_state.get("folder") or task.get("last_path") or ""
        self.detail_path.setText(f"保存位置：{folder}" if folder else "保存位置：尚未开始记录")
        committed = recording_state.get("committed")
        self.detail_frames.setText(f"已保存帧数：{committed}" if committed is not None else "")
        update_recording_frame(self, task)


class DeviceCategoryGroup(QFrame):
    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("monitorCategory")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 10)
        self.heading = QLabel(title)
        self.heading.setObjectName("monitorCategoryTitle")
        layout.addWidget(self.heading)
        self.grid = CategoryGrid()
        layout.addWidget(self.grid)

    def set_cards(self, cards: Sequence[QWidget]) -> None:
        self.grid.set_items(cards)


class CategoryGrid(QWidget):
    """A growing grid used inside the single overview scroll area."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.layout_grid = QGridLayout(self)
        self.layout_grid.setContentsMargins(0, 0, 0, 0)
        self.layout_grid.setSpacing(16)
        self.layout_grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.items: list[QWidget] = []
        self.columns = 0
        self.installEventFilter(self)

    def eventFilter(self, obj, event):  # noqa: N802
        if obj is self and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self.reflow)
        return super().eventFilter(obj, event)

    def set_items(self, items: Sequence[QWidget]) -> None:
        self.items = list(items)
        self.reflow(force=True)
        QTimer.singleShot(0, self.reflow)

    def reflow(self, force: bool = False) -> None:
        columns = max(1, (self.width() + 16) // 376)
        if columns == self.columns and not force:
            return
        old_columns = self.columns
        self.columns = columns
        while self.layout_grid.count():
            self.layout_grid.takeAt(0)
        for index in range(max(old_columns, columns)):
            self.layout_grid.setColumnStretch(index, 1 if index < columns else 0)
            self.layout_grid.setColumnMinimumWidth(index, 0)
        for index, item in enumerate(self.items):
            self.layout_grid.addWidget(item, index // columns, index % columns, Qt.AlignmentFlag.AlignTop)


class MonitoringDashboard(QWidget):
    open_card = Signal(object)

    def __init__(self, controller: WorkspaceController, parent=None) -> None:
        super().__init__(parent)
        self.controller, self.mode = controller, ""
        self.specs: list[dict[str, Any]] = []
        self.cards: dict[str, RecordingDeviceCard] = {}
        self.groups: dict[str, DeviceCategoryGroup] = {}
        self.preferences: dict[str, dict[str, Any]] = {}
        self.legacy_preferences: dict[str, dict[str, dict[str, Any]]] = {}
        layout = QVBoxLayout(self)
        heading = QLabel("实时监测总览")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        toolbar = QHBoxLayout()
        toolbar.addStretch()
        self.sample_button = QPushButton("一键开始采样")
        self.sample_button.setObjectName("primaryButton")
        self.sample_button.clicked.connect(self.toggle_sampling)
        toolbar.addWidget(self.sample_button)
        layout.addLayout(toolbar)
        self.hint = QLabel()
        self.hint.setObjectName("overviewNote")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.grid = ResponsiveGrid()
        layout.addWidget(self.grid, 1)
        controller.refreshed.connect(self.refresh)
        controller.monitoring_interval_changed.connect(self._sync_interval_from_timing)
        self.refresh()

    def _slot_key(self, record: dict[str, Any]) -> str:
        mode = self._recording_mode(record)
        slot = record.get("slots", {}).get(mode, "")
        return f"slot:{mode}:{slot}" if slot else f"device:{record['id']}"

    def _recording_mode(self, record: dict[str, Any]) -> str:
        return "general"

    def _load_preferences(self) -> None:
        raw = self.controller.runtime.repository.load_workspace("monitor_overview/all", {})
        self.preferences = dict(raw) if isinstance(raw, dict) else {}
        self.legacy_preferences = {}
        for mode in ("general",):
            legacy = self.controller.runtime.repository.load_workspace(f"monitor_overview/{mode}", {})
            self.legacy_preferences[mode] = dict(legacy) if isinstance(legacy, dict) else {}

    def _persist(self) -> None:
        self.controller.runtime.repository.save_workspace("monitor_overview/all", self.preferences)

    def _channels(self, device_id: str, record: dict[str, Any]) -> tuple[int, ...]:
        category = instrument_category(record)
        if category not in {"oscilloscope", "power_supply", "optical_power_meter", "frequency_counter"}:
            return (1,)
        try:
            channels = tuple(self.controller.capabilities(device_id).channels)
        except Exception:
            channels = (1,)
        return (
            scope_channels(self.controller, device_id, channels) if category == "oscilloscope" else channels
        )

    def _recording_sources(self, record: dict[str, Any]):
        sources = data_sources(config_from_record(record))
        category = instrument_category(record)
        return (
            sources
            if category in {"power_supply", "signal_generator", "network_analyzer", "phase_noise_analyzer", "frequency_counter"}
            else sources[:1]
        )

    def _saved_preference(self, record: dict[str, Any]) -> dict[str, Any]:
        key = self._slot_key(record)
        saved = self.preferences.get(key)
        if isinstance(saved, dict):
            return saved
        mode = self._recording_mode(record)
        legacy = self.legacy_preferences.get(mode, {})
        slot = record.get("slots", {}).get(mode, "")
        legacy_keys = (
            f"slot:{mode}:{slot}" if slot else "",
            f"slot:{slot}" if slot else "",
            slot,
            f"device:{record['id']}",
            record["id"],
        )
        for legacy_key in legacy_keys:
            if legacy_key and isinstance(legacy.get(legacy_key), dict):
                migrated = dict(legacy[legacy_key])
                self.preferences[key] = migrated
                return migrated
        saved = {}
        self.preferences[key] = saved
        return saved

    def _spec_for(self, record: dict[str, Any]) -> dict[str, Any] | None:
        device_id = record["id"]
        try:
            sources = self._recording_sources(record)
        except Exception:
            return None
        if not sources:
            return None
        source = sources[0]
        saved = self._saved_preference(record)
        recording_mode = self._recording_mode(record)
        # Older versions stored the overview interval independently.  Use it
        # once when no device timing exists, then make the device timing key
        # canonical so both pages keep reading the same value afterwards.
        timing_key = f"monitor-timing/{recording_mode}/{device_id}"
        stored_timing = self.controller.runtime.repository.load_workspace(timing_key, None)
        memory_timing = self.controller.runtime.monitoring_preferences.get(timing_key)
        has_timing = isinstance(stored_timing, dict) and "interval" in stored_timing
        has_timing = has_timing or isinstance(memory_timing, dict) and "interval" in memory_timing
        if not has_timing and "interval" in saved:
            try:
                save_monitoring_interval(
                    self.controller,
                    device_id,
                    recording_mode,
                    float(saved["interval"]),
                    notify=False,
                )
            except (TypeError, ValueError):
                pass
        try:
            default_interval = float(
                monitoring_options(self.controller, device_id, recording_mode)["interval"]
            )
        except Exception:
            default_interval = 1.0
        interval = float(saved.get("interval", default_interval))
        interval = min(60.0, max(0.5, interval))
        if instrument_category(record) == "power_supply":
            interval = min(POWER_INTERVALS, key=lambda option: abs(option - interval))
        saved["interval"] = interval
        saved.setdefault("enabled", True)
        identity = record.get("identity") or {}
        maker = identity.get("manufacturer", "")
        model = identity.get("model", "")
        serial = identity.get("serial", "")
        details = " · ".join(value for value in (maker, model, serial) if value) or device_label(record)
        slot = record.get("slots", {}).get(recording_mode, "")
        return {
            "id": f"device:{device_id}",
            "device_id": device_id,
            "source": source.key,
            "sources": [item.key for item in sources],
            "channels": list(self._channels(device_id, record)),
            "title": device_label(record),
            "category": instrument_category(record),
            "category_label": CATEGORY_LABELS.get(instrument_category(record), instrument_category(record)),
            "identity": details,
            "slot": slot,
            "slot_label": slot_label(slot),
            "enabled": bool(saved.get("enabled", True)),
            "interval": interval,
            "recording_mode": recording_mode,
        }

    def _sync_interval_from_timing(self, device_id: str, interval: float) -> None:
        """Reflect a device-page edit in the matching overview card."""
        card = self.cards.get(device_id)
        record = self.controller.records.get(device_id)
        if card is None or record is None:
            return
        interval = float(interval)
        if instrument_category(record) == "power_supply":
            interval = min(POWER_INTERVALS, key=lambda option: abs(option - interval))
        card.interval.blockSignals(True)
        card.interval.setValue(interval)
        card.interval.blockSignals(False)
        key = self._slot_key(record)
        preference = self.preferences.setdefault(key, {})
        preference["interval"] = interval
        for spec in self.specs:
            if spec.get("device_id") == device_id:
                spec["interval"] = interval
        self._persist()

    def refresh(self) -> None:
        if self.mode != self.controller.mode:
            self.mode = self.controller.mode
            self._load_preferences()
        specs = []
        for record in self.controller.connected_records():
            spec = self._spec_for(record)
            if spec is not None:
                specs.append(spec)
        active_ids = {spec["device_id"] for spec in specs}
        for device_id in set(self.cards) - active_ids:
            card = self.cards.pop(device_id)
            card.hide()
            card.deleteLater()
        self.specs = specs
        grouped: dict[str, list[RecordingDeviceCard]] = {}
        for spec in specs:
            existing_card = self.cards.get(spec["device_id"])
            if existing_card is None:
                card = RecordingDeviceCard(self.controller, spec)
                card.changed.connect(self._persist_card)
                card.open_requested.connect(self._open_device)
                self.cards[spec["device_id"]] = card
            else:
                card = existing_card
                card.set_spec(spec)
            grouped.setdefault(spec["category"], []).append(card)
            card.refresh()
        for category, group in list(self.groups.items()):
            if category not in grouped:
                group.hide()
                group.deleteLater()
                del self.groups[category]
        ordered_groups = []
        for category in CATEGORY_LABELS:
            if category not in grouped:
                continue
            existing_group = self.groups.get(category)
            if existing_group is None:
                group = DeviceCategoryGroup(CATEGORY_LABELS[category])
                self.groups[category] = group
            else:
                group = existing_group
            group.set_cards(grouped[category])
            group.show()
            ordered_groups.append(group)
        for category, cards in grouped.items():
            if category in self.groups and self.groups[category] in ordered_groups:
                continue
            existing_group = self.groups.get(category)
            if existing_group is None:
                group = DeviceCategoryGroup(CATEGORY_LABELS.get(category, category))
                self.groups[category] = group
            else:
                group = existing_group
            group.set_cards(cards)
            group.show()
            ordered_groups.append(group)
        self.grid.set_items(ordered_groups)
        self._update_button()
        self.hint.setText(
            f"当前显示 {len(specs)} 台已连接设备 · 已勾选 {sum(bool(s.get('enabled')) for s in specs)} 台"
        )

    def _persist_card(self) -> None:
        for spec in self.specs:
            card = self.cards.get(spec["device_id"])
            if card is None:
                continue
            key = self._slot_key(self.controller.records[spec["device_id"]])
            interval = card.interval.value()
            if spec["category"] == "power_supply":
                interval = min(POWER_INTERVALS, key=lambda option: abs(option - interval))
                if interval != card.interval.value():
                    card.interval.blockSignals(True)
                    card.interval.setValue(interval)
                    card.interval.blockSignals(False)
            try:
                save_monitoring_interval(
                    self.controller,
                    spec["device_id"],
                    spec["recording_mode"],
                    interval,
                )
            except Exception as exc:
                self.controller.message.emit(str(exc))
            self.preferences[key] = {
                "enabled": card.enabled.isChecked(),
                "interval": interval,
            }
            spec["enabled"] = card.enabled.isChecked()
            spec["interval"] = interval
            if not card.enabled.isChecked() and self.controller.runtime.monitoring.snapshots().get(
                spec["device_id"], {}
            ).get("running"):
                self.controller.watch(self.controller.runtime.monitoring.stop(spec["device_id"]))
        self._persist()
        self._update_button()

    def _open_device(self, device_id: str) -> None:
        record = self.controller.records.get(device_id)
        if not record:
            return
        spec = next((item for item in self.specs if item["device_id"] == device_id), None)
        self.controller.navigate.emit("实时监测", instrument_category(record), device_id)
        self.open_card.emit(spec or {"device_id": device_id, "source": "", "channels": [1]})

    def add(
        self, device_id: str, source: str, channels: tuple[int, ...], title: str = "", view: str = ""
    ) -> None:
        """Compatibility entry for type pages; overview cards are automatic now."""
        self.refresh()
        card = self.cards.get(device_id)
        if card is None:
            return
        target = next((spec for spec in self.specs if spec["device_id"] == device_id), None)
        if target is not None:
            task = self.controller.runtime.monitoring.snapshots().get(device_id, {})
            if task.get("running") and task.get("interval") is not None:
                # Pinning from a live page must carry its current scheduler
                # setting into the overview preference before the card is
                # persisted.  This avoids reviving an older overview value on
                # the next stop/start cycle.
                target["interval"] = float(task["interval"])
            target.update(
                source=source,
                channels=list(channels),
                title=title or target["title"],
                view=view,
            )
            card.set_spec(target)
            # A few integrations still inspect the legacy per-source view of
            # ``specs`` after pinning a page.  Keep source aliases transiently;
            # the next connected-device refresh rebuilds the canonical one-card
            # list, and requests are deduplicated below.
            if len(target.get("sources", ())) > 1:
                aliases = []
                for source_key in target["sources"]:
                    if source_key == target["source"]:
                        continue
                    alias = dict(target)
                    alias["id"] = f"{target['id']}:{source_key}"
                    alias["source"] = source_key
                    aliases.append(alias)
                self.specs.extend(aliases)
        self._persist_card()

    def card_action(self, action: str, card_id: str) -> None:
        device_id = card_id.removeprefix("device:")
        if action == "open" and device_id in self.cards:
            self._open_device(device_id)

    def _requests(self) -> tuple[list[tuple[str, tuple[AcquisitionRequest, ...], float, str]], list[str]]:
        grouped = []
        dangerous = []
        seen_devices: set[str] = set()
        for spec in self.specs:
            card = self.cards.get(spec["device_id"])
            record = self.controller.records.get(spec["device_id"])
            if (
                not card
                or not record
                or not card.enabled.isChecked()
                or not self.controller.has_connected_session(spec["device_id"])
            ):
                continue
            if spec["device_id"] in seen_devices:
                continue
            seen_devices.add(spec["device_id"])
            channels = tuple(spec["channels"])
            if not channels:
                continue
            available_sources = self._recording_sources(record)
            wanted = set(spec.get("sources", (spec["source"],)))
            sources = [item for item in available_sources if item.key in wanted]
            if not sources:
                self.controller.message.emit(f"{spec['title']} 暂无可用数据源")
                continue
            initiate = record.get("adapterId") in {"pico.6404e", "keysight.3458a"}
            requests = tuple(
                source.request(channels, initiate=initiate, confirmed=initiate) for source in sources
            )
            grouped.append((record["id"], requests, card.interval.value(), self._recording_mode(record)))
            if initiate:
                dangerous.append(device_label(record))
        return grouped, dangerous

    def toggle_sampling(self) -> None:
        active = [
            device_id
            for device_id in self.cards
            if self.controller.runtime.monitoring.snapshots().get(device_id, {}).get("running")
        ]
        if active:
            self.stop()
            return
        grouped, dangerous = self._requests()
        if not grouped:
            self.controller.message.emit("请至少勾选一台已连接设备")
            return
        if (
            dangerous
            and QMessageBox.question(
                self,
                "确认开始采样",
                "以下设备会启动仪器采样：\n" + "\n".join(dangerous),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        for device_id, requests, interval, recording_mode in grouped:
            try:
                record = self.controller.records[device_id]
                if not mode_applicable(record, recording_mode):
                    raise DeviceError("此设备没有可用的监测模式", "unsupported")
                options = monitoring_options(self.controller, device_id, recording_mode)
                options["interval"] = interval
                options["record_interval"] = None
                self.controller.runtime.monitoring.start(
                    device_id,
                    requests,
                    **options,
                    mode=recording_mode,
                    recording=True,
                )
            except Exception as exc:
                self.controller.message.emit(f"{self.controller.records[device_id]['adapterId']}：{exc}")
        self._update_button()

    def start(self) -> None:
        self.toggle_sampling()

    def stop(self) -> None:
        for device_id in tuple(self.cards):
            if self.controller.runtime.monitoring.snapshots().get(device_id, {}).get("running"):
                self.controller.watch(self.controller.runtime.monitoring.stop(device_id))
        self._update_button()

    def _update_button(self) -> None:
        active = any(
            self.controller.runtime.monitoring.snapshots().get(device_id, {}).get("running")
            for device_id in self.cards
        )
        self.sample_button.setText("停止采样" if active else "一键开始采样")
