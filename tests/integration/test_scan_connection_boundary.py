import threading

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.discovery.presence import is_current
from lab_control.discovery.service import DiscoveryCandidate, Inventory
from lab_control.domain.instruments import DeviceError, DeviceIdentity
from lab_control.storage.repository import Repository
from lab_control.ui.real_window import RealWindow


def test_connect_is_blocked_through_enumeration_and_probe_gaps(qtbot, tmp_path, monkeypatch):
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    config = ConnectionConfig("mock://scan-boundary", "rigol.dsa815", "mock")
    key = runtime.add(config)
    candidate = DiscoveryCandidate(config, "configured")
    identity = DeviceIdentity("RIGOL", "DSA815", "FIXTURE")
    runtime.inventory.merge(candidate, "old", identity, config.adapter)
    entered, release, probing, finish = (threading.Event() for _ in range(4))
    opens = []
    factory = runtime.sessions.factory
    monkeypatch.setattr(runtime.sessions, "factory", lambda c: (opens.append(c), factory(c))[1])

    class PausedProvider:
        name = "configured"

        def enumerate(self):
            entered.set()
            assert release.wait(5)
            return (candidate,)

    def probe(_candidate):
        probing.set()
        assert finish.wait(5)
        return identity, config.adapter

    runtime.discovery.providers = (PausedProvider(),)
    runtime.discovery.probe = probe
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.timer.stop()
    window.controller.timer.stop()
    try:
        window.open_category("spectrum_analyzer")
        window.tree.setCurrentItem(window.rows[key])
        future = runtime.discovery.scan()
        assert entered.wait(2)
        window.refresh()
        assert not window.action_buttons["connect"].isEnabled()
        assert "扫描" in window.action_buttons["connect"].toolTip()
        with pytest.raises(DeviceError) as exc:
            runtime.connect(key)
        assert exc.value.code == "scan_busy" and opens == []
        with pytest.raises(DeviceError) as exc:
            runtime.select_resource(key, config.resource)
        assert exc.value.code == "scan_busy"
        release.set()
        assert probing.wait(2)
        with pytest.raises(DeviceError) as exc:
            runtime.connect(key)
        assert exc.value.code == "scan_busy" and opens == []
        finish.set()
        future.result(3)
        window.refresh()
        assert window.selected_id == key and window.action_buttons["connect"].isEnabled()
        runtime.connect(key).result(3)
        assert len(opens) == 1
    finally:
        release.set()
        finish.set()
        window.closing = True
        window.controller.close()
        window.bridge.worker.shutdown()
        runtime.close()


def test_failed_fresh_lan_probe_keeps_row_selection_and_error(qtbot, tmp_path):
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    config = ConnectionConfig("TCPIP0::192.0.2.10::inst0::INSTR", "rigol.dsa815")
    candidate = DiscoveryCandidate(config, "lan", safe_scpi=True)
    identity = DeviceIdentity("RIGOL", "DSA815", "FIXTURE")
    record = runtime.inventory.merge(candidate, "old", identity, config.adapter, reason="identified")
    key = record["id"]
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.timer.stop()
    window.controller.timer.stop()
    try:
        window.open_category("spectrum_analyzer")
        window.tree.setCurrentItem(window.rows[key])
        row = window.rows[key]
        runtime.inventory.merge(candidate, "new", reason="pending_probe")
        window.refresh()
        runtime.inventory.merge(candidate, "new", error="VISA open failed", reason="probe_failed")
        runtime.inventory.stale("new", {"lan"})
        window.refresh()
        assert window.rows[key] is row and not row.isHidden() and window.selected_id == key
        assert "探测失败" in row.text(3)
        assert window.details.values["error"].text() == "VISA open failed"
        assert window.details.problem.text() == "异常：VISA open failed"
        assert not window.details.problem.isHidden()
        assert window.details.values["driver"].text() == "待核对（探测失败）"
        # Older saved probe errors also cannot prove a driver failure.
        runtime.inventory.records[key]["driverStatus"] = "error"
        window.refresh()
        assert window.details.values["driver"].text() == "待核对（探测失败）"
        assert not window.action_buttons["monitor"].isEnabled()
        assert is_current(runtime.inventory.all()[0])
        # Restart and later absent scans must not retain stale announcements as current.
        assert not is_current(Inventory(runtime.repository).all()[0])
        runtime.inventory.stale("later", {"lan"})
        assert not is_current(runtime.inventory.all()[0])
        runtime.inventory.merge(candidate, "recovered", identity, config.adapter, reason="identified")
        window.refresh()
        assert window.details.problem.isHidden()
    finally:
        window.closing = True
        window.controller.close()
        window.bridge.worker.shutdown()
        runtime.close()


def test_scan_finalization_preserves_newer_connection_evidence(tmp_path):
    repo = Repository(tmp_path)
    inventory = Inventory(repo)
    config = ConnectionConfig("TCPIP0::192.0.2.10::inst0::INSTR", "rigol.dsa815")
    identity = DeviceIdentity("RIGOL", "DSA815", "FIXTURE")
    try:
        inventory.merge(DiscoveryCandidate(config, "lan"), "scan", identity, config.adapter)
        inventory.merge(DiscoveryCandidate(config, "configured"), "connected", identity, config.adapter)
        inventory.stale("scan", {"lan"})
        assert inventory.all()[0]["presenceState"] == "present"
    finally:
        repo.close()
