from pathlib import Path

import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QFont

from lab_control.app import load_stylesheet, load_ui_font
from lab_control.application.runtime import ApplicationRuntime
from lab_control.ui.pages.storage_settings import StorageSettingsPanel
from lab_control.ui.real_window import RealWindow


@pytest.fixture
def settings_window(qtbot, qapp, tmp_path):
    style, font = qapp.styleSheet(), qapp.font()
    qapp.setFont(QFont(load_ui_font(), 10))
    qapp.setStyleSheet(load_stylesheet())
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    window.timer.stop()
    window.navigate("系统设置", "", "")
    qtbot.wait(20)
    yield window, runtime
    window.closing = True
    window.hide()
    window.storage_panel.shutdown()
    window.environment_panel.shutdown()
    window.bridge.worker.shutdown()
    runtime.close()
    qapp.setStyleSheet(style)
    qapp.setFont(font)


def test_directory_browse_save_open_and_reload(settings_window, qtbot, monkeypatch, tmp_path):
    window, runtime = settings_window
    panel = window.storage_panel
    chosen = tmp_path / "中文 保存目录"
    monkeypatch.setattr(
        "lab_control.ui.pages.storage_settings.QFileDialog.getExistingDirectory", lambda *args: str(chosen)
    )
    qtbot.mouseClick(panel.browse_button, Qt.MouseButton.LeftButton)
    assert Path(panel.directory.text()) == chosen
    qtbot.mouseClick(panel.save_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: panel.status.text().startswith("已保存"))
    effective = chosen / "simulation"
    assert runtime.repository.data_root == effective
    assert str(effective) in panel.effective.text() and "模拟" in panel.effective.text()
    opened = []
    monkeypatch.setattr(
        "lab_control.ui.pages.storage_settings.QDesktopServices.openUrl",
        lambda url: opened.append(Path(url.toLocalFile())) or True,
    )
    qtbot.mouseClick(panel.open_button, Qt.MouseButton.LeftButton)
    assert opened == [effective]
    restored = StorageSettingsPanel(runtime.repository)
    qtbot.addWidget(restored)
    assert Path(restored.directory.text()) == chosen
    restored.shutdown()


def test_invalid_directory_shows_failure_and_keeps_effective_path(settings_window, qtbot, tmp_path):
    window, runtime = settings_window
    panel, previous = window.storage_panel, runtime.repository.data_root
    panel.directory.clear()
    panel.save()
    assert "请输入" in panel.status.text()
    bad = tmp_path / "not-a-directory"
    bad.write_text("keep me", encoding="utf-8")
    panel.directory.setText(str(bad))
    panel.save()
    qtbot.waitUntil(lambda: panel.status.text().startswith("保存失败"))
    assert runtime.repository.data_root == previous
    assert panel.directory.isEnabled() and panel.save_button.isEnabled()
    assert bad.read_text(encoding="utf-8") == "keep me"


@pytest.mark.parametrize("width,height", [(860, 600), (1000, 650), (1440, 900)])
def test_settings_remain_reachable_at_small_and_large_sizes(settings_window, qtbot, width, height):
    window, _ = settings_window
    window.fit_to_screen(QRect(0, 0, width, height))
    window.resize(width - 4, height - 4)
    qtbot.wait(30)
    assert window.width() <= width and window.height() <= height
    assert window.stack.currentWidget() is window.settings_page
    scroll, panel = window.settings_page, window.storage_panel
    assert scroll.horizontalScrollBar().maximum() == 0
    scroll.ensureWidgetVisible(panel.save_button)
    qtbot.wait(10)
    rect = panel.save_button.rect()
    center = panel.save_button.mapTo(scroll.viewport(), rect.center())
    assert scroll.viewport().rect().contains(center)
    assert panel.directory.width() >= 120
    scroll.ensureWidgetVisible(window.environment_panel.export_button)
    qtbot.wait(10)
    center = window.environment_panel.export_button.mapTo(
        scroll.viewport(), window.environment_panel.export_button.rect().center()
    )
    assert scroll.viewport().rect().contains(center)
