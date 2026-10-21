import threading
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QPushButton

from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceError
from lab_control.ui.pages import component_preparation as ui


def runtime(root):
    value = SimpleNamespace(
        configurations=(),
        inventory=SimpleNamespace(all=lambda: ()),
        repository=SimpleNamespace(root=root),
        hardware=True,
        active=False,
    )
    value.begin_maintenance = lambda: setattr(value, "active", True)
    value.end_maintenance = lambda: setattr(value, "active", False)
    return value


def test_selection_download_and_explicit_installer_click(qtbot, tmp_path, monkeypatch):
    calls, opened = [], []
    state = {"value": "missing"}

    def inspect(*args, **kwargs):
        return {"items": [{"id": "msvc-runtime", "state": state["value"], "reason": "fixture"}]}

    def prepare(ids, **kwargs):
        calls.append(ids)
        kwargs["progress"]({"id": "msvc-runtime", "state": "prepared"})
        return {"passed": True}

    rt = runtime(tmp_path)
    panel = ui.ComponentPreparationPanel(rt, preparer=prepare, inspector=inspect, launcher=opened.append)
    qtbot.addWidget(panel)
    monkeypatch.setattr(ui, "verify_component", lambda *a: {"state": "verified"})
    monkeypatch.setattr(ui, "check_file", lambda *a: None)
    try:
        assert panel.selected() == []
        assert calls == opened == []
        panel.prepare()
        assert not calls
        panel.rows["msvc-runtime"].setCheckState(0, Qt.CheckState.Checked)
        panel.table.setCurrentItem(panel.rows["msvc-runtime"])
        panel.prepare_button.click()
        qtbot.waitUntil(panel.prepare_button.isEnabled)
        assert calls == [["msvc-runtime"]]
        assert opened == [] and not rt.active
        assert "未安装" in panel.rows["msvc-runtime"].text(2)
        panel.install_button.click()
        qtbot.waitUntil(lambda: len(opened) == 1)
        assert rt.active
        panel.recheck_button.click()
        qtbot.waitUntil(panel.prepare_button.isEnabled)
        assert not rt.active
        assert "失败" in panel.rows["msvc-runtime"].text(2)
        assert "未完成" in panel.status.text()
        state["value"] = "ok"
        panel.prepare_button.click()
        qtbot.waitUntil(panel.prepare_button.isEnabled)
        assert "设备未验收" in panel.rows["msvc-runtime"].text(2)
        assert calls == [["msvc-runtime"]]  # Already detected compatible runtime is reused.
        assert not panel.install_button.isEnabled()
    finally:
        panel.shutdown()


def test_busy_session_or_failed_verification_cannot_launch(qtbot, tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    opened = []
    panel = ui.ComponentPreparationPanel(rt, inspector=lambda *a, **k: {"items": []}, launcher=opened.append)
    qtbot.addWidget(panel)
    panel.table.setCurrentItem(panel.rows["msvc-runtime"])
    panel.results["msvc-runtime"] = {"state": "prepared"}
    try:
        rt.begin_maintenance = lambda: (_ for _ in ()).throw(DeviceError("设备正在使用", "maintenance_busy"))
        panel.install()
        assert "正在使用" in panel.status.text()
        assert not opened and not panel.installing
        rt.begin_maintenance = lambda: setattr(rt, "active", True)
        monkeypatch.setattr(ui, "verify_component", lambda *a: {"state": "invalid", "error": "hash failed"})
        panel.install()
        qtbot.waitUntil(lambda: not panel.installing)
        assert not opened and not rt.active
        assert "hash failed" in panel.status.text()
    finally:
        panel.shutdown()


def test_empty_inventory_selects_by_model_without_implicit_download(qtbot, tmp_path):
    panel = ui.ComponentPreparationPanel(runtime(tmp_path), preparer=lambda *a, **k: pytest.fail("download"))
    qtbot.addWidget(panel)
    try:
        assert panel.selected() == []
        panel.device_choice.setCurrentIndex(panel.device_choice.findText("PicoScope 6404E / PicoSDK"))
        assert set(panel.selected()) == {"msvc-runtime", "pico-sdk"}
        assert not panel.busy()
    finally:
        panel.shutdown()


def test_first_run_can_skip_without_download_and_is_not_repeated(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "lab_control.deployment.materials.download_file", lambda *a, **k: pytest.fail("implicit download")
    )
    rt = runtime(tmp_path)

    def skip():
        dialog = QApplication.activeModalWidget()
        next(b for b in dialog.findChildren(QPushButton) if "稍后" in b.text()).click()

    QTimer.singleShot(0, skip)
    ui.show_preparation(rt, None, first_run=True)
    assert (tmp_path / "maintenance/first-run.json").exists()
    ui.show_preparation(rt, None, first_run=True)
    (tmp_path / "maintenance/first-run.json").unlink()
    rt.hardware = False
    ui.show_preparation(rt, None, first_run=True)
    assert not (tmp_path / "maintenance/first-run.json").exists()


@pytest.mark.parametrize("activity", ["scan", "sessions", "recording", "batch"])
def test_runtime_maintenance_reservation_blocks_active_hardware(activity):
    # Exercise real admission methods without constructing any hardware backends.
    rt = ApplicationRuntime.__new__(ApplicationRuntime)
    rt.closed = False
    rt.sessions = SimpleNamespace(lock=threading.RLock(), sessions={}, maintenance_active=False)
    rt.discovery = SimpleNamespace(
        lock=threading.RLock(), is_scanning=False, closing=False, connection_batch_active=False, future=None
    )
    rt.monitoring = SimpleNamespace(snapshots=lambda: {})
    if activity == "scan":
        rt.discovery.is_scanning = True
    elif activity == "sessions":
        rt.sessions.sessions["fixture"] = object()
    elif activity == "recording":
        rt.monitoring.snapshots = lambda: {"fixture": {"recording": True}}
    else:
        rt.discovery.connection_batch_active = True
    with pytest.raises(DeviceError):
        rt.begin_maintenance()
    assert not rt.sessions.maintenance_active
    rt.discovery.is_scanning = rt.discovery.connection_batch_active = False
    rt.sessions.sessions.clear()
    rt.monitoring.snapshots = lambda: {}
    rt.begin_maintenance()
    with pytest.raises(DeviceError, match="安装向导"):
        rt._check_connection_admission()
    from lab_control.discovery.service import DiscoveryService

    rt.discovery.sessions = rt.sessions
    with pytest.raises(DeviceError, match="安装向导"):
        DiscoveryService.scan(rt.discovery)
    rt.end_maintenance()
    assert not rt.sessions.maintenance_active
