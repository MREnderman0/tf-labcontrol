from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from lab_control.demo.store import DemoStore
from lab_control.domain.models import DeviceCategory
from lab_control.ui.widgets.device_card import DeviceCard


class OverviewPage(QFrame):
    category_clicked = Signal(DeviceCategory)

    MAIN_CATEGORIES = (
        DeviceCategory.SIGNAL_GENERATOR,
        DeviceCategory.POWER_SUPPLY,
        DeviceCategory.OSCILLOSCOPE,
        DeviceCategory.SPECTRUM_ANALYZER,
        DeviceCategory.TEMPERATURE_CONTROLLER,
    )

    def __init__(self, store: DemoStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("overviewPage")
        self.store = store
        self.cards: dict[DeviceCategory, DeviceCard] = {}
        self._cards_layout: QGridLayout
        self._build()
        self.store.changed.connect(self.refresh)
        self.refresh()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(30, 24, 30, 24)
        outer.setSpacing(14)

        eyebrow = QLabel("设备管理 / 总览")
        eyebrow.setObjectName("eyebrow")
        title = QLabel("设备总览")
        title.setObjectName("pageTitle")
        subtitle = QLabel("按设备类型查看当前模式下的设备池、连接状态和功能槽位。")
        subtitle.setObjectName("pageDescription")
        outer.addWidget(eyebrow)
        outer.addWidget(title)
        outer.addWidget(subtitle)

        note = QLabel("设计演示 · 卡片、模式过滤和详情抽屉均为内存状态，不会连接真实设备。")
        note.setObjectName("designNote")
        outer.addWidget(note)

        scroll = QScrollArea()
        scroll.setObjectName("contentScroll")
        scroll.setWidgetResizable(True)
        scroll.viewport().setObjectName("contentViewport")
        scroll.viewport().setAutoFillBackground(False)
        body = QWidget()
        body.setObjectName("scrollBody")
        body.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._cards_layout = QGridLayout(body)
        self._cards_layout.setContentsMargins(0, 0, 0, 0)
        self._cards_layout.setHorizontalSpacing(28)
        self._cards_layout.setVerticalSpacing(20)
        self._cards_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        trend = QFrame()
        trend.setObjectName("trendPlaceholder")
        trend_layout = QVBoxLayout(trend)
        trend_layout.setContentsMargins(14, 10, 14, 10)
        trend_layout.setSpacing(2)
        trend_title = QLabel("实时趋势预览")
        trend_title.setObjectName("panelTitle")
        trend_text = QLabel("后续接入 pyqtgraph：这里将展示温度、电压、功率或频谱趋势。")
        trend_text.setObjectName("mutedText")
        trend_layout.addWidget(trend_title)
        trend_layout.addWidget(trend_text)
        outer.addWidget(trend)

    def refresh(self) -> None:
        while self._cards_layout.count():
            item = self._cards_layout.takeAt(0)
            widget = item.widget() if item else None
            if widget:
                widget.deleteLater()
        self.cards.clear()

        categories = list(self.MAIN_CATEGORIES)
        if self.store.mode.value == "optical":
            categories.append(DeviceCategory.OPTICAL_POWER_METER)
        for index, category in enumerate(categories):
            card = DeviceCard(self.store.category_stats(category))
            card.clicked.connect(self.category_clicked)
            self.cards[category] = card
            self._cards_layout.addWidget(card, index // 3, index % 3)
        for column in range(3):
            self._cards_layout.setColumnStretch(column, 1)
