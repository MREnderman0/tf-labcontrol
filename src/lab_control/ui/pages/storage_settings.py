"""Data destination settings, separate from application inventory and logs."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from lab_control.storage.repository import Repository


class StorageSettingsPanel(QWidget):
    completed = Signal(str, bool)

    def __init__(self, repository: Repository, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.repository = repository
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="storage-settings")
        self.job: Future | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 10, 0, 10)
        heading = QLabel("数据保存一级目录")
        heading.setObjectName("panelTitle")
        layout.addWidget(heading)
        row = QHBoxLayout()
        self.directory = QLineEdit(str(repository.data_root_base))
        self.directory.setMinimumWidth(0)
        self.directory.setPlaceholderText("输入绝对路径，或浏览选择目录")
        row.addWidget(self.directory, 1)
        self.browse_button = QPushButton("浏览…")
        self.browse_button.clicked.connect(self.browse)
        row.addWidget(self.browse_button)
        self.save_button = QPushButton("保存设置")
        self.save_button.setObjectName("primaryButton")
        self.save_button.clicked.connect(self.save)
        row.addWidget(self.save_button)
        self.open_button = QPushButton("打开目录")
        self.open_button.clicked.connect(self.open_directory)
        row.addWidget(self.open_button)
        layout.addLayout(row)
        self.effective = QLabel()
        self.effective.setWordWrap(True)
        layout.addWidget(self.effective)
        example = QLabel(
            "单设备采集 / YYYY-MM-DD / HH-mm-ss-SSS_型号_序列号_continuous_批次标识\n"
            "单帧保存使用 single_frame；日期以开始记录或保存时的本地时间为准。\n"
            "修改仅影响之后开始的记录；已有文件和正在记录的路径保持不变。"
        )
        example.setWordWrap(True)
        example.setObjectName("pageDescription")
        layout.addWidget(example)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.completed.connect(self._completed)
        self.refresh()

    def refresh(self) -> None:
        suffix = "（模拟数据独立保存）" if self.repository.simulation else ""
        self.effective.setText(f"实际保存目录：{self.repository.data_root}{suffix}")

    def browse(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择数据保存一级目录", self.directory.text())
        if selected:
            self.directory.setText(selected)

    def save(self) -> None:
        if self.job is not None and not self.job.done():
            return
        value = self.directory.text().strip()
        if not value:
            self.status.setText("请输入数据保存目录")
            return
        self.directory.setEnabled(False)
        self.browse_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.status.setText("正在检查目录并保存设置…")
        self.job = self.worker.submit(self.repository.set_data_root, Path(value))

        def finished(job: Future) -> None:
            try:
                job.result()
            except Exception as exc:
                self.completed.emit(f"保存失败，原设置保持不变：{exc}", False)
            else:
                self.completed.emit("已保存；从下一次记录或单帧保存开始生效。", True)

        self.job.add_done_callback(finished)

    def _completed(self, message: str, success: bool) -> None:
        self.directory.setEnabled(True)
        self.browse_button.setEnabled(True)
        self.save_button.setEnabled(True)
        self.status.setText(message)
        if success:
            self.directory.setText(str(self.repository.data_root_base))
        self.refresh()

    def open_directory(self) -> None:
        folder = self.repository.data_root
        if not folder.is_dir():
            self.status.setText("目录尚未创建，请先保存设置或开始记录。")
        elif not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            self.status.setText("无法打开目录，请检查路径是否可用。")

    def shutdown(self) -> None:
        self.worker.shutdown(wait=True)
