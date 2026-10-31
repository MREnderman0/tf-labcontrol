"""History list and per-device offline data workbench."""

from __future__ import annotations

from concurrent.futures import Future
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lab_control.domain.processing import RecordingEntry
from lab_control.storage.processing import current_result, result_home
from lab_control.ui.pages.processing_viewer import ProcessingViewer
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox


class DataProcessingPage(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.service = controller.runtime.processing
        self.scope = "single_device"
        self.entries: tuple[RecordingEntry, ...] = ()
        self.entry: RecordingEntry | None = None
        self.scan_future: Future | None = None
        self.scan_again = False
        self.operations: list[Future] = []
        self.rows: dict[str, tuple] = {}
        self.date_groups: dict[tuple[str, str], QTreeWidgetItem] = {}
        self.date_expanded: dict[tuple[str, str], bool] = {}
        self.last_jobs = {}
        self.pending_destination: Path | None = None
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        self.title = QLabel("单设备采集")
        self.title.setObjectName("pageTitle")
        header.addWidget(self.title)
        header.addStretch()
        add = QPushButton("添加数据目录")
        add.clicked.connect(self.add_root)
        header.addWidget(add)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        header.addWidget(refresh)
        layout.addLayout(header)
        self.message = QLabel("历史记录离线处理，不访问仪器。")
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        self.message.setObjectName("mutedText")
        layout.addWidget(self.message)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["采集名称", "开始时间（含时区）", "参与设备", "处理状态", "操作"])
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for index, width in ((1, 225), (2, 180), (3, 130), (4, 480)):
            self.tree.setColumnWidth(index, width)
        self.tree.itemClicked.connect(
            lambda item, column: (
                self.open_entry(item.data(0, Qt.ItemDataRole.UserRole))
                if column != 4 and isinstance(item.data(0, Qt.ItemDataRole.UserRole), RecordingEntry)
                else None
            )
        )
        self.stack.addWidget(self.tree)
        detail = QWidget()
        body = QVBoxLayout(detail)
        body.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        back = QPushButton("返回历史列表")
        back.clicked.connect(lambda: self.stack.setCurrentIndex(0))
        bar.addWidget(back)
        self.devices = WorkspaceComboBox()
        self.devices.currentIndexChanged.connect(self.show_device)
        bar.addWidget(self.devices, 1)
        self.process_button = QPushButton("处理本次采集")
        self.process_button.clicked.connect(lambda: self.process(self.entry) if self.entry else None)
        bar.addWidget(self.process_button)
        self.cancel_button = QPushButton("取消处理")
        self.cancel_button.clicked.connect(lambda: self.service.cancel(self.entry) if self.entry else None)
        self.cancel_button.hide()
        bar.addWidget(self.cancel_button)
        self.source_folder_button = QPushButton("打开文件夹")
        self.source_folder_button.clicked.connect(lambda: self.open_folder(self.entry))
        bar.addWidget(self.source_folder_button)
        open_results = QPushButton("打开处理目录")
        open_results.clicked.connect(self.open_results_folder)
        bar.addWidget(open_results)
        body.addLayout(bar)
        self.detail_label = QLabel()
        self.detail_label.setTextFormat(Qt.TextFormat.PlainText)
        self.detail_label.setWordWrap(True)
        body.addWidget(self.detail_label)
        self.progress = QProgressBar()
        self.progress.hide()
        body.addWidget(self.progress)
        self.viewer = ProcessingViewer(self.service)
        self.viewer.export_requested.connect(self.export_figure)
        body.addWidget(self.viewer, 1)
        self.stack.addWidget(detail)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(200)

    def set_scope(self, scope: str) -> None:
        self.scope = scope if scope in {"single_device", "batch"} else "single_device"
        self.title.setText("单设备采集" if self.scope == "single_device" else "批量采集")
        self.message.setText("历史记录离线处理，不访问仪器。")
        self.stack.setCurrentIndex(0)
        self.refresh()

    def refresh(self) -> None:
        if self.scan_future is not None:
            self.scan_again = True
            return
        if not self.service.closed:
            self.scan_future = self.service.read_worker.submit(self.service.catalog.scan)

    def add_root(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "添加历史采集目录")
        if folder:
            try:
                self.service.catalog.add_root(Path(folder))
                self.refresh()
            except Exception as exc:
                self.message.setText(str(exc))

    def rebuild(self) -> None:
        self.date_expanded.update({key: item.isExpanded() for key, item in self.date_groups.items()})
        self.date_groups.clear()
        self.tree.clear()
        self.rows.clear()
        groups: dict[str, list[tuple[RecordingEntry, str]]] = {}
        for entry in self.entries:
            if entry.scope != self.scope:
                continue
            try:
                moment = datetime.fromisoformat(entry.started)
                stamp = moment.isoformat(sep=" ", timespec="seconds")
                date = moment.date().isoformat()
            except ValueError:
                stamp = entry.started
                date = "时间未知"
            groups.setdefault(date, []).append((entry, stamp))
        for date in sorted(groups, key=lambda date: (date != "时间未知", date), reverse=True):
            group = QTreeWidgetItem([f"{date} · {len(groups[date])} 条采集"])
            font = group.font(0)
            font.setBold(True)
            group.setFont(0, font)
            group.setSizeHint(0, QSize(0, 36))
            self.tree.addTopLevelItem(group)
            group.setFirstColumnSpanned(True)
            key = (self.scope, date)
            self.date_groups[key] = group
            group.setExpanded(self.date_expanded.get(key, True))
            for entry, stamp in groups[date]:
                self._add_row(group, entry, stamp)
        self.update_states()
        if not self.rows:
            self.message.setText("此分类暂无采集记录，可添加历史数据目录。")

    def _add_row(self, group: QTreeWidgetItem, entry: RecordingEntry, stamp: str) -> None:
        item = QTreeWidgetItem([entry.name, stamp, "、".join(d.label for d in entry.devices), "", ""])
        item.setData(0, Qt.ItemDataRole.UserRole, entry)
        for i in range(3):
            item.setToolTip(i, item.text(i))
        group.addChild(item)
        actions = QWidget()
        row = QHBoxLayout(actions)
        row.setContentsMargins(4, 3, 4, 3)
        row.setSpacing(5)
        buttons = []
        for label, handler in (
            ("处理数据", self.process),
            ("移动", self.move_entry),
            ("移入回收站", self.trash),
            ("重命名", self.rename),
            ("打开文件夹", self.open_folder),
        ):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, e=entry, action=handler: action(e))
            if label == "移入回收站":
                button.setToolTip("将本次采集及处理结果一起移入系统回收站")
            row.addWidget(button)
            buttons.append(button)
        cancel = QPushButton("取消")
        cancel.clicked.connect(lambda _checked=False, e=entry: self.service.cancel(e))
        row.addWidget(cancel)
        cancel.hide()
        self.tree.setItemWidget(item, 4, actions)
        item.setSizeHint(0, QSize(0, max(48, actions.sizeHint().height())))
        self.rows[entry.key] = (entry, item, buttons, cancel)

    def update_states(self) -> None:
        jobs = self.service.snapshot()
        for key, (entry, item, buttons, cancel) in self.rows.items():
            job = jobs.get(key, {})
            busy = job.get("state") in {"queued", "running"}
            reason = ""
            try:
                self.service.check_idle(entry)
            except Exception as exc:
                reason = str(exc)
            process_allowed = True
            if reason:
                try:
                    self.service.check_idle(entry, allow_corrupt=True)
                except Exception:
                    process_allowed = False
            completed = 0
            damaged = ""
            for device in entry.devices:
                try:
                    completed += current_result(result_home(entry.folder, device)) is not None
                except (OSError, ValueError, TypeError) as exc:
                    damaged = f"结果索引损坏，可重新处理：{exc}"
            status = f"已处理 {completed}/{len(entry.devices)}" if completed else "未处理"
            if damaged:
                status = damaged
            if busy:
                status = f"{job.get('device') or '后台任务'} · {job['done']}/{job['total'] or '…'}"
            elif job.get("state") in {"failed", "cancelled"}:
                status = job.get("error", "处理失败")
            elif any(result.get("error") for result in job.get("results", [])):
                status = "部分失败 · " + "；".join(r["error"] for r in job["results"] if r["error"])
            elif reason:
                status = reason
            item.setText(3, status)
            item.setToolTip(3, status)
            for button in buttons:
                button.setEnabled(not busy and not reason)
            buttons[0].setEnabled(not busy and process_allowed)
            buttons[-1].setEnabled(entry.folder.is_dir())
            cancel.setVisible(busy and job.get("action") == "process")
            if self.entry and self.entry.key == key:
                self.process_button.setEnabled(not busy and process_allowed)
                self.cancel_button.setVisible(busy and job.get("action") == "process")
                self.progress.setVisible(busy)
                if busy:
                    self.progress.setRange(0, max(0, job["total"]))
                    self.progress.setValue(job["done"])
                    self.progress.setFormat(
                        f"{job.get('device') or '后台任务'} · {job.get('stage', '整理数据')} · "
                        f"{job['done']:,}/{job['total']:,} {job.get('unit', '帧')}"
                    )

    def poll(self) -> None:
        if self.service.closed:
            self.timer.stop()
            return
        if self.scan_future is not None and self.scan_future.done():
            task, self.scan_future = self.scan_future, None
            try:
                self.entries = task.result()
                self.rebuild()
                if self.pending_destination is not None:
                    target = next((e for e in self.entries if e.folder == self.pending_destination), None)
                    if target:
                        self.open_entry(target)
                    self.pending_destination = None
            except Exception as exc:
                self.message.setText(f"历史记录读取失败：{exc}")
            if self.scan_again:
                self.scan_again = False
                self.refresh()
        remaining = []
        for task in self.operations:
            if not task.done():
                remaining.append(task)
                continue
            try:
                result = task.result()
                errors = [r["error"] for r in result if r.get("error")] if isinstance(result, list) else []
                self.message.setText(
                    "；".join(errors) if errors else "操作完成；原始时间和设备身份保持不变。"
                )
                if isinstance(result, dict) and result.get("figure"):
                    self.message.setText("论文插图已保存。")
                if isinstance(result, dict) and result.get("destination") and self.stack.currentIndex() == 1:
                    self.pending_destination = Path(result["destination"])
                if isinstance(result, dict) and "trash" in result:
                    self.stack.setCurrentIndex(0)
                    self.entry = None
                    self.viewer.clear()
                if (
                    self.entry
                    and self.stack.currentIndex() == 1
                    and not (isinstance(result, dict) and result.get("figure"))
                ):
                    self.show_device()
            except Exception as exc:
                self.message.setText(str(exc))
            self.refresh()
        self.operations = remaining
        if self.isVisible():
            self.update_states()

    def open_entry(self, entry: RecordingEntry) -> None:
        self.entry = entry
        self.detail_label.setText(f"{entry.name}\n采集开始：{entry.started} · {len(entry.devices)} 台设备")
        self.detail_label.setToolTip(str(entry.folder))
        self.devices.blockSignals(True)
        self.devices.clear()
        for index, device in enumerate(entry.devices):
            identity = device.metadata.get("identity") or {}
            self.devices.addItem(f"{device.label} · {identity.get('serial') or device.folder.name}", index)
        self.devices.blockSignals(False)
        self.stack.setCurrentIndex(1)
        self.show_device()
        self.update_states()

    def open_folder(self, entry: RecordingEntry | None) -> None:
        if entry is not None:
            self._open_path(entry.folder)

    def _open_path(self, folder: Path) -> None:
        if not folder.is_dir():
            self.message.setText("文件夹已不存在，请刷新历史列表。")
        elif not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            self.message.setText("无法打开文件夹，请检查文件资源管理器。")

    def open_results_folder(self) -> None:
        if self.entry is None:
            return
        folder = self.entry.folder / "数据处理"
        self._open_path(folder if folder.is_dir() else self.entry.folder)

    def show_device(self) -> None:
        if self.entry is None or self.devices.currentIndex() < 0:
            return
        device = self.entry.devices[self.devices.currentIndex()]
        try:
            result = current_result(result_home(self.entry.folder, device))
            if result:
                self.viewer.open_result(result)
            else:
                outcomes = self.service.snapshot().get(self.entry.key, {}).get("results", [])
                error = next((r["error"] for r in outcomes if r["source"] == str(device.folder)), "")
                self.viewer.clear(device.error or error or "该设备尚无处理结果，点击“处理本次采集”生成。")
        except Exception as exc:
            self.viewer.clear(str(exc))

    def process(self, entry: RecordingEntry) -> None:
        self._run(lambda: self.service.process(entry))

    def export_figure(self, payload: dict) -> None:
        if self.entry:
            self._run(lambda: self.service.export_figure(self.entry, payload))

    def _run(self, action) -> None:
        try:
            self.operations.append(action())
            self.update_states()
        except Exception as exc:
            self.message.setText(str(exc))

    def move_entry(self, entry: RecordingEntry) -> None:
        destination = QFileDialog.getExistingDirectory(self, "移动整个采集文件夹到…")
        if destination:
            self._run(lambda: self.service.move(entry, Path(destination)))

    def rename(self, entry: RecordingEntry) -> None:
        name, accepted = QInputDialog.getText(self, "重命名采集文件夹", "文件夹名称", text=entry.name)
        if accepted:
            self._run(lambda: self.service.rename(entry, name))

    def trash(self, entry: RecordingEntry) -> None:
        answer = QMessageBox.question(
            self,
            "移入回收站",
            f"将“{entry.name}”及其中所有原始数据和处理结果移入系统回收站？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._run(lambda: self.service.trash(entry))
