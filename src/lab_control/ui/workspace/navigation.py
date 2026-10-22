from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtWidgets import (
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.catalog import instrument_category, surface_applicable
from lab_control.application.controls import CATEGORY_LABELS
from lab_control.discovery.presence import LIVE_CONNECTIONS
from lab_control.ui.pages.device_control.coremorrow_e00 import CoreMorrowE00Page
from lab_control.ui.pages.device_control.page import DeviceControlPage
from lab_control.ui.pages.device_control.power_supply import PowerSupplyPage
from lab_control.ui.pages.device_control.signal_source import SignalSourcePage
from lab_control.ui.pages.monitoring.frequency_counter import FrequencyCounterMonitoringPage
from lab_control.ui.pages.monitoring.optical_power import OpticalPowerMonitoringPage
from lab_control.ui.pages.monitoring.optical_spectrum import OpticalSpectrumMonitoringPage
from lab_control.ui.pages.monitoring.oscilloscope import OscilloscopeMonitoringPage
from lab_control.ui.pages.monitoring.page import MonitoringPage
from lab_control.ui.pages.monitoring.phase_noise import PhaseNoiseMonitoringPage
from lab_control.ui.pages.monitoring.power import PowerMonitoringPage
from lab_control.ui.pages.monitoring.spectrum import SpectrumMonitoringPage

if TYPE_CHECKING:
    from lab_control.ui.real_window import RealWindow


class WorkspaceNavigation:
    """Page selection using the controller's cached workspace."""

    def __init__(self, window: RealWindow) -> None:
        self.window = window

    def focus_card(self, spec: dict) -> None:
        window = self.window
        page = window.stack.currentWidget()
        if isinstance(
            page,
            (
                MonitoringPage,
                SpectrumMonitoringPage,
                OscilloscopeMonitoringPage,
                PowerMonitoringPage,
                OpticalPowerMonitoringPage,
                OpticalSpectrumMonitoringPage,
                FrequencyCounterMonitoringPage,
            ),
        ):
            page.focus_card(spec)

    def _connected_categories(
        self,
        records: dict[str, dict[str, Any]] | None = None,
        states: dict[str, dict[str, Any]] | None = None,
    ) -> frozenset[str]:
        window = self.window
        if records is None:
            records = window.controller.records
        if states is None:
            states = window.controller.states
        return frozenset(
            instrument_category(record)
            for device_id, record in records.items()
            if states.get(device_id, {}).get("connectionState") in LIVE_CONNECTIONS
        )

    def _submenu_categories(
        self,
        module: str,
        records: dict[str, dict[str, Any]] | None = None,
        states: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[str, ...]:
        window = self.window
        if module not in {"设备管理", "实时监测"}:
            return ()
        monitoring_only = {
            "spectrum_analyzer",
            "oscilloscope",
            "phase_noise_analyzer",
            "optical_power_meter",
            "optical_spectrometer",
            "frequency_counter",
        }
        if records is None:
            records = window.controller.records
        if states is None:
            states = window.controller.states
        connected = tuple(
            record
            for device_id, record in records.items()
            if states.get(device_id, {}).get("connectionState") in LIVE_CONNECTIONS
        )
        available = {instrument_category(record) for record in connected}
        return tuple(
            value
            for value in CATEGORY_LABELS
            if value in available
            and not (module == "设备管理" and value in monitoring_only)
            and (
                value != "power_supply"
                or any(
                    surface_applicable(
                        record,
                        "management" if module == "设备管理" else "monitoring",
                    )
                    for record in connected
                    if instrument_category(record) == value
                )
            )
        )

    def _rebuild_submenu(
        self,
        module: str,
        category: str = "",
        records: dict[str, dict[str, Any]] | None = None,
        states: dict[str, dict[str, Any]] | None = None,
    ) -> str:
        window = self.window
        categories = window._submenu_categories(module, records, states)
        selected_category = category if category in categories else ""
        window.submenu.blockSignals(True)
        while window.submenu.count():
            window.submenu.removeTab(0)
        if module in {"设备管理", "实时监测"}:
            window.submenu.addTab("总览")
            window.submenu.setTabData(0, "")
            selected_index = 0
            for value in categories:
                index = window.submenu.addTab(CATEGORY_LABELS[value])
                window.submenu.setTabData(index, value)
                if value == selected_category:
                    selected_index = index
            window.submenu.setCurrentIndex(selected_index)
        else:
            window.submenu.addTab("软件设置" if module == "系统设置" else module)
            window.submenu.setTabData(0, "")
        window.submenu.blockSignals(False)
        window._submenu_signature = (module, categories)
        return selected_category

    def _sync_submenu(self) -> None:
        window = self.window
        if window.current_module not in {"设备管理", "实时监测"}:
            return
        categories = window._submenu_categories(window.current_module, window.records, window.states)
        current_category = window.submenu.tabData(window.submenu.currentIndex()) or ""
        if (window.current_module, categories) != window._submenu_signature or (
            current_category and current_category not in categories
        ):
            window.navigate(
                window.current_module,
                current_category if current_category in categories else "",
            )

    def navigate(self, module: str, category: str = "", device_id: str = "") -> None:
        window = self.window
        if module == "设备管理" and category in {
            "spectrum_analyzer",
            "oscilloscope",
            "phase_noise_analyzer",
            "optical_power_meter",
            "optical_spectrometer",
            "frequency_counter",
        }:
            module = "实时监测"
        elif module == "设备管理" and category == "power_supply":
            records = window.controller.records
            states = window.controller.states
            requested = records.get(device_id)
            has_management_supply = (
                requested is not None
                and states.get(device_id, {}).get("connectionState") in LIVE_CONNECTIONS
                and instrument_category(requested) == "power_supply"
                and surface_applicable(requested, "management")
                if device_id
                else any(
                    states.get(record_id, {}).get("connectionState") in LIVE_CONNECTIONS
                    and instrument_category(record) == "power_supply"
                    and surface_applicable(record, "management")
                    for record_id, record in records.items()
                )
            )
            if not has_management_supply:
                module = "实时监测"
        if module not in window.nav_buttons:
            return
        window.device_pool.hide()
        window.current_module = module
        for name, button in window.nav_buttons.items():
            button.setChecked(name == module)
        window.section_title.setText(module)
        window._rebuild_submenu(module, category)
        window.show_page(device_id=device_id)

    def show_page(self, *_args, device_id: str = "") -> None:
        window = self.window
        window.device_pool.hide()
        module, category = window.current_module, window.submenu.tabData(window.submenu.currentIndex()) or ""
        window.breadcrumb.setText(f"{module} / {window.submenu.tabText(window.submenu.currentIndex())}")
        if module == "设备管理" and not category:
            page = window.overview_page
        elif module == "实时监测" and not category:
            page = window.dashboard
        elif module == "系统设置":
            page = window.settings_page
        else:
            key = (module, category)
            page = window.page_map.get(key)
            if page is None:
                if module == "设备管理":
                    page = (
                        SignalSourcePage(window.controller)
                        if category == "signal_generator"
                        else CoreMorrowE00Page(window.controller)
                        if category == "power_supply"
                        else DeviceControlPage(window.controller, category)
                    )
                elif module == "实时监测":
                    page = (
                        SpectrumMonitoringPage(window.controller, window.dashboard.add)
                        if category == "spectrum_analyzer"
                        else OscilloscopeMonitoringPage(window.controller, window.dashboard.add)
                        if category == "oscilloscope"
                        else PowerMonitoringPage(window.controller, window.dashboard.add)
                        if category == "power_supply"
                        else OpticalPowerMonitoringPage(window.controller, window.dashboard.add)
                        if category == "optical_power_meter"
                        else OpticalSpectrumMonitoringPage(window.controller, window.dashboard.add)
                        if category == "optical_spectrometer"
                        else PhaseNoiseMonitoringPage(window.controller)
                        if category == "phase_noise_analyzer"
                        else FrequencyCounterMonitoringPage(window.controller, window.dashboard.add)
                        if category == "frequency_counter"
                        else MonitoringPage(window.controller, category, window.dashboard.add)
                    )
                else:
                    page = QWidget()
                    layout = QVBoxLayout(page)
                    title = QLabel(module)
                    title.setObjectName("pageTitle")
                    layout.addWidget(title)
                    message = {
                        "数据处理": "数据处理工作台尚未开放。已保存数据可使用 CSV / NPZ 导出文件。",
                        "操作日志": "设备操作、监测与异常按日保存在软件运行目录的 logs 文件夹。",
                    }[module]
                    layout.addWidget(QLabel(message))
                    button = QPushButton("进入实时监测")
                    button.clicked.connect(lambda: window.navigate("实时监测"))
                    layout.addWidget(button)
                    layout.addStretch()
                window.page_map[key] = page
                window.stack.addWidget(page)
        assert page is not None
        if device_id and isinstance(
            page,
            (
                DeviceControlPage,
                MonitoringPage,
                SpectrumMonitoringPage,
                OscilloscopeMonitoringPage,
                PowerMonitoringPage,
                OpticalPowerMonitoringPage,
                OpticalSpectrumMonitoringPage,
                PhaseNoiseMonitoringPage,
                FrequencyCounterMonitoringPage,
                SignalSourcePage,
                PowerSupplyPage,
                CoreMorrowE00Page,
            ),
        ):
            # Resolve a requested device before showEvent can start readback for the page.
            page.selector.select(device_id)
        window.stack.setCurrentWidget(page)
