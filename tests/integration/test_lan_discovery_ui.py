"""Twenty mock LAN assets retain UI selection and drafts after an IP change."""

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.discovery.service import DiscoveryCandidate
from lab_control.domain.instruments import DeviceIdentity
from lab_control.ui.real_window import RealWindow


def test_twenty_lan_assets_keep_rows_selection_and_draft(qtbot, tmp_path):
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.timer.stop()
    window.show()

    def merge(index, base, scan):
        config = ConnectionConfig(f"TCPIP0::192.0.2.{base + index}::inst0::INSTR", "rigol.dg5504pro")
        return runtime.inventory.merge(
            DiscoveryCandidate(config, "lan", safe_scpi=True),
            scan,
            DeviceIdentity("RIGOL", "DG5504 Pro", f"FIXTURE_{index}"),
            config.adapter,
            reason="identified",
        )

    try:
        original = [merge(index, 1, "first") for index in range(20)]
        chosen = original[8]["id"]
        runtime.repository.save_workspace("draft/" + chosen, {"frequency": "1234"})
        window.refresh()
        window.open_category("signal_generator")
        rows = dict(window.rows)
        window.tree.setCurrentItem(rows[chosen])
        window.tree.verticalScrollBar().setValue(6)
        position = window.tree.verticalScrollBar().value()
        for index in range(20):
            assert merge(index, 101, "second")["id"] == original[index]["id"]
        runtime.inventory.stale("second", {"lan"})
        window.refresh()
        assert len(window.rows) == 20 and all(window.rows[key] is row for key, row in rows.items())
        assert window.tree.currentItem() is rows[chosen]
        assert window.tree.verticalScrollBar().value() == position
        assert runtime.repository.load_workspace("draft/" + chosen) == {"frequency": "1234"}
        assert not runtime.sessions.sessions
    finally:
        window.closing = True
        window.hide()
        window.bridge.worker.shutdown()
        runtime.close()
