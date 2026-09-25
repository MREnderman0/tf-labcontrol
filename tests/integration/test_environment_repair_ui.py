import json
import threading
from types import SimpleNamespace

from lab_control.ui.pages.environment_repair import EnvironmentRepairPanel


def runtime(root):
    return SimpleNamespace(
        configurations=(),
        inventory=SimpleNamespace(all=lambda: ()),
        repository=SimpleNamespace(root=root),
        hardware=False,
        lan_settings=SimpleNamespace(enabled=False),
    )


def item(state):
    return {
        "id": state,
        "title": state,
        "state": state,
        "reason": "检查结果",
        "action": "建议操作",
        "affected": ["型号"],
        "requires_admin": True,
        "reboot": "possible",
        "payload": {"state": "missing"},
    }


def test_background_check_cancel_keeps_results_and_exports(qtbot, tmp_path):
    entered = threading.Event()

    def inspect(root, configs, *, cancel, progress, **kwargs):
        report = {
            "schema_version": 1,
            "kind": "environment_inspection",
            "state": "running",
            "items": [item(s) for s in ("ok", "missing", "incompatible", "unknown", "not_required", "error")],
        }
        progress(report)
        entered.set()
        assert cancel.wait(5)
        return dict(report, state="cancelled")

    panel = EnvironmentRepairPanel(runtime(tmp_path), inspector=inspect)
    qtbot.addWidget(panel)
    try:
        panel.start()
        qtbot.waitUntil(entered.is_set)
        qtbot.waitUntil(lambda: panel.table.topLevelItemCount() == 6)
        assert not panel.check_button.isEnabled()
        panel.cancel()
        qtbot.waitUntil(panel.check_button.isEnabled)
        assert panel.report["state"] == "cancelled"
        assert panel.plan_button.isEnabled()
        path = tmp_path / "report.json"
        panel.save_report(path)
        value = json.loads(path.read_text(encoding="utf-8"))
        assert len(value["items"]) == 6
        assert not value["repair_plan"]["executed"]
        assert not value["repair_plan"]["inspection_complete"]
    finally:
        panel.shutdown()


def test_failed_check_keeps_partial_report_and_can_retry(qtbot, tmp_path):
    def inspect(root, configs, *, progress, **kwargs):
        progress({"state": "running", "items": [item("ok")]})
        raise OSError("fixture failure")

    panel = EnvironmentRepairPanel(runtime(tmp_path), inspector=inspect)
    qtbot.addWidget(panel)
    try:
        panel.start()
        qtbot.waitUntil(panel.check_button.isEnabled)
        assert panel.report["state"] == "error"
        assert len(panel.report["items"]) == 1
        assert "fixture failure" in panel.status.text()
        assert panel.export_button.isEnabled()
    finally:
        panel.shutdown()


def test_completed_check_has_no_fake_repair_action(qtbot, tmp_path):
    panel = EnvironmentRepairPanel(
        runtime(tmp_path), inspector=lambda *a, **k: {"state": "complete", "items": [item("ok")]}
    )
    qtbot.addWidget(panel)
    try:
        panel.start()
        qtbot.waitUntil(panel.check_button.isEnabled)
        assert "尚未执行" in panel.status.text()
        from PySide6.QtWidgets import QPushButton

        assert all(button.text() != "执行修复" for button in panel.findChildren(QPushButton))
    finally:
        panel.shutdown()
