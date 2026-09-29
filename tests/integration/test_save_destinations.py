import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lab_control.application.monitoring import MonitorFrame
from lab_control.cli.main import main
from lab_control.domain.instruments import AcquisitionRequest, DeviceError
from lab_control.storage.repository import Repository

from .test_monitoring_timing import connected, wait_for

START = datetime(2026, 9, 27, 23, 59, 59, 999000, tzinfo=timezone(timedelta(hours=8)))
REQUEST = AcquisitionRequest("spectrum")


@pytest.mark.parametrize("action,kind", [("acquire", "single_frame"), ("stream", "continuous")])
def test_cli_uses_persisted_data_root_and_shared_naming(tmp_path, capsys, monkeypatch, action, kind):
    repo = Repository(tmp_path / "simulation", simulation=True)
    chosen = tmp_path / "chosen"
    repo.set_data_root(chosen)
    repo.close()
    monkeypatch.setattr("lab_control.storage.paths.local_now", lambda: START)
    args = [
        "--mock",
        "--home",
        str(tmp_path),
        "--resource",
        "mock://saved-cli",
        "--adapter",
        "rigol.dsa815",
        action,
        "--kind",
        "spectrum",
        "--mode",
        "optical",
    ]
    if action == "stream":
        args += ["--count", "1"]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    folder = Path(result["path"]) if action == "acquire" else Path(result["paths"][0]).parent.parent
    assert folder.is_relative_to(chosen / "simulation" / "单设备采集" / "2026-09-27")
    assert f"_{kind}_" in folder.name
    metadata = json.loads((folder / "_raw" / "recording.json").read_text(encoding="utf-8"))
    assert metadata["mode"] == "optical" and metadata["identity"]["model"] == "DSA815"
    assert Path(result["report"]).is_relative_to(tmp_path / "simulation" / "reports")


@pytest.mark.parametrize("entry", ["displayed", "frame", "fresh_read"])
def test_queued_single_save_pins_root_and_start_but_preserves_frame_time(tmp_path, monkeypatch, entry):
    runtime, key = connected(tmp_path)
    with runtime:
        repo, service = runtime.repository, runtime.monitoring
        repo.set_data_root(tmp_path / "初始 数据")
        original_root = repo.data_root
        clock = [START]
        monkeypatch.setattr("lab_control.storage.paths.local_now", lambda: clock[0])
        adapter = runtime.sessions.sessions[key].adapter
        old = replace(adapter.acquire(REQUEST), timestamp="2025-01-02T03:04:05+00:00")
        entered, release = threading.Event(), threading.Event()
        export = repo.export

        def delayed_export(batch, mode, **options):
            entered.set()
            assert release.wait(5)
            return export(batch, mode, **options)

        def delayed_read(request):
            entered.set()
            assert release.wait(5)
            return old

        if entry == "fresh_read":
            monkeypatch.setattr(adapter, "acquire", delayed_read)
            future = service.capture_and_export(key, REQUEST, "optical")
        else:
            monkeypatch.setattr(repo, "export", delayed_export)
            frame = MonitorFrame(1, 0, REQUEST, old)
            future = (
                service.export_displayed_frame(key, frame, "optical")
                if entry == "displayed"
                else service.export(frame, "optical")
            )
        try:
            assert entered.wait(3)
            clock[0] += timedelta(seconds=2)
            repo.set_data_root(tmp_path / "次日 数据")
            if entry != "frame":
                with pytest.raises(DeviceError, match="正在"):
                    service.export_displayed_frame(key, MonitorFrame(1, 0, REQUEST, old), "optical")
        finally:
            release.set()
        folder = future.result(5)
        assert folder.is_relative_to(original_root / "单设备采集" / "2026-09-27")
        assert folder.name.startswith("23-59-59-999_") and "_single_frame_" in folder.name
        manifest = json.loads((folder / "_raw" / "recording.json").read_text(encoding="utf-8"))
        saved = json.loads((folder / "_raw" / "snapshot.json").read_text(encoding="utf-8"))
        assert manifest["started_local"] == START.isoformat() and manifest["mode"] == "optical"
        assert manifest["identity"]["serial"] == old.identity.serial
        assert saved["identity"] == manifest["identity"]
        if entry == "fresh_read":
            assert saved["timestamp"] == saved["timing"]["host_read_completed_at"]
            assert saved["timestamp"] != old.timestamp
        else:
            assert saved["timestamp"] == old.timestamp
        assert saved["timestamp"] in (folder / "记录说明.txt").read_text(encoding="utf-8-sig")
        assert not list(repo.data_root.rglob("recording.json"))
        wait_for(lambda: not service.capture_saves and not service.display_exports)


def test_continuous_start_pins_destination_and_repeat_start_keeps_same_run(tmp_path, monkeypatch):
    runtime, key = connected(tmp_path)
    with runtime:
        repo, service = runtime.repository, runtime.monitoring
        clock = [START]
        monkeypatch.setattr("lab_control.storage.paths.local_now", lambda: clock[0])
        original_root = repo.data_root
        service.start(key, (REQUEST,), interval=0.05)
        service.set_recording(key, True, mode="optical")
        first = service.tasks[key].recorder
        assert (first.folder / "_raw" / "recording.json").is_file()
        wait_for(lambda: first.snapshot()["committed"] >= 1)
        clock[0] += timedelta(seconds=2)
        repo.set_data_root(tmp_path / "new-root")
        service.set_recording(key, True)
        assert service.tasks[key].recorder is first
        assert first.folder.is_relative_to(original_root / "单设备采集" / "2026-09-27")
        before = first.snapshot()["committed"]
        wait_for(lambda: first.snapshot()["committed"] > before)
        service.stop(key).result(5)
        first.finished.result(5)
        assert list(first.raw_folder.glob("segment-*.sqlite3")) and list(first.folder.glob("数据_*.csv"))
        assert not runtime.sessions.snapshots()[0]["controlsLocked"]
        service.start(key, (REQUEST,), interval=0.05)
        service.set_recording(key, True)
        second = service.tasks[key].recorder
        service.stop(key).result(5)
        second.finished.result(5)
        assert second.folder.is_relative_to(repo.data_root / "单设备采集" / "2026-09-28")
        assert first.folder != second.folder and "_continuous_" in second.folder.name


@pytest.mark.parametrize("failure", ["directory", "manifest", "session_error"])
def test_recording_preparation_failure_releases_reservation_and_keeps_monitor_mode(
    tmp_path, monkeypatch, failure
):
    runtime, key = connected(tmp_path)
    with runtime:
        service, session = runtime.monitoring, runtime.sessions.sessions[key]
        service.start(key, (REQUEST,), interval=0.05)
        open_path = Path.open
        mkdir = Path.mkdir

        def fail_open(path, *args, **kwargs):
            if path.name == "recording.json":
                raise PermissionError("manifest denied")
            return open_path(path, *args, **kwargs)

        def fail_mkdir(path, *args, **kwargs):
            if "_continuous_" in path.name:
                if failure == "session_error":
                    session.state = "error"
                raise PermissionError("directory denied")
            return mkdir(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", fail_open)
        if failure != "manifest":
            monkeypatch.setattr(Path, "mkdir", fail_mkdir)
        with pytest.raises(PermissionError, match="denied"):
            service.set_recording(key, True, mode="optical")
        task = service.tasks[key]
        assert not task.recording and task.recorder is None and not task.record_session
        assert task.mode == "electronic" and not session.recording
        assert not runtime.sessions.snapshots()[0]["controlsLocked"]
        service.stop(key).result(5)


def test_direct_acquisition_pins_context_before_read_and_invalid_root_leaves_no_task(tmp_path, monkeypatch):
    runtime, key = connected(tmp_path)
    with runtime:
        repo, sessions = runtime.repository, runtime.sessions
        session = sessions.sessions[key]
        repo.data_root.mkdir(parents=True)
        repo.data_root.rmdir()
        repo.data_root.write_text("path is now a file", encoding="utf-8")
        with pytest.raises(OSError):
            sessions.start_acquisition(key, REQUEST, count=1)
        assert not session.pending and session.direct_recording is None and session.state == "connected"
        repo.set_data_root(tmp_path / "working")
        original_root = repo.data_root
        clock = [START]
        monkeypatch.setattr("lab_control.storage.paths.local_now", lambda: clock[0])
        read = session.adapter.acquire
        entered, release = threading.Event(), threading.Event()

        def delayed(request):
            entered.set()
            assert release.wait(5)
            return read(request)

        monkeypatch.setattr(session.adapter, "acquire", delayed)
        future = sessions.start_acquisition(key, REQUEST, count=1)
        try:
            assert entered.wait(3)
            clock[0] += timedelta(seconds=2)
            repo.set_data_root(tmp_path / "next")
        finally:
            release.set()
        future.result(8)
        recording = sessions.recordings[-1]
        assert recording.folder.is_relative_to(original_root / "单设备采集" / "2026-09-27")
        assert not session.pending and not sessions.snapshots()[0]["controlsLocked"]
