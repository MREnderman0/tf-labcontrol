import json

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.cli.main import main
from lab_control.ui.real_window import CATEGORIES, RealWindow


def test_cli_mock_readonly_report_and_explicit_target(tmp_path, capsys):
    common = ["--mock", "--home", str(tmp_path)]
    assert main(common + ["identity"]) == 2
    assert "device_required" in capsys.readouterr().out
    args = common + ["--resource", "mock://one", "--adapter", "rigol.dg5504pro"]
    assert main(args + ["test"]) == 0
    assert len(list(tmp_path.rglob("reports/*.json"))) == 1
    assert main(args + ["acquire", "--kind", "waveform", "--points", "16"]) == 0


def test_scan_stress_reports_live_sessions_and_exported_capture(tmp_path, capsys):
    config = tmp_path / "instruments.toml"
    config.write_text(
        "\n".join(
            f'[[devices]]\nresource="mock://{i}"\nadapter="rigol.dg5504pro"\ntransport="mock"'
            for i in range(6)
        ),
        encoding="utf-8",
    )
    assert (
        main(
            [
                "--mock",
                "--home",
                str(tmp_path),
                "--config",
                str(config),
                "scan-stress",
                "--resources",
                *(f"mock://{i}" for i in range(6)),
                "--capture-resource",
                "mock://0",
                "--kind",
                "waveform",
                "--points",
                "16",
                "--repeats",
                "4",
            ]
        )
        == 0
    )
    report = json.loads(next(tmp_path.rglob("reports/*.json")).read_text(encoding="utf-8"))
    assert report["software_checks_passed"] and not report["hardware_accepted"]
    assert len(report["before"]) == len(report["after"]) == 6
    assert report["capture_paths"]
    assert all(
        item["reason"] == "cached_active_session" for scan in report["scans"] for item in scan["items"]
    )
    capsys.readouterr()


def test_real_overview_incremental_mode_and_scan(qtbot, tmp_path):
    config = ConnectionConfig("mock://one", "rigol.dg5504pro", "mock")
    runtime = ApplicationRuntime((config,), root=tmp_path, hardware=False)
    device_id = runtime.add(config)
    runtime.connect(device_id).result()
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    window.refresh()
    assert len(window.cards) == len(CATEGORIES) == 8
    row = window.rows[device_id]
    window.tree.setCurrentItem(row)
    session_id = runtime.sessions.snapshots()[0]["sessionId"]
    window.scan()
    qtbot.waitUntil(lambda: runtime.discovery.future.done(), timeout=3000)
    window.refresh()
    window.mode.setCurrentIndex(1)
    assert window.rows[device_id] is row
    assert window.selected_id == device_id
    assert runtime.sessions.snapshots()[0]["sessionId"] == session_id
    assert window.action_buttons["disconnect"].isEnabled()
    window.timer.stop()
    runtime.close()
    window.closing = True
    window.bridge.worker.shutdown()
