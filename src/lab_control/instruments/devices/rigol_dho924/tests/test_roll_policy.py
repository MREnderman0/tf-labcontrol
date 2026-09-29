"""MAIN policy, read-only boundaries, serial recovery and cache publication."""

import threading

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError

from ..adapter import DHO924Adapter
from ..profile import profile
from .fake import Peer, adapter
from .test_control_cache import prepare_auto
from .test_sessions import runtime as runtime
from .test_sessions import wait_for
from .test_workspace import page as page


def roll(peer):
    peer.state.update({":TIM:MODE": "ROLL", ":TIM:ROLL": 1, ":TIM:MAIN:SCAL": 20})


@pytest.mark.parametrize("auto_roll", [0, 1])
def test_timebase_limit_is_independent_of_auto_roll_preference(auto_roll):
    obj, peer = adapter()
    peer.state[":TIM:ROLL"] = auto_roll
    obj.refresh_capabilities()
    assert obj.capabilities_for_channel(1).get("timebase").parameters["value"].maximum == 0.02
    peer.history.clear()
    for value in (0.05, 1, 500):
        with pytest.raises(DeviceError, match="above"):
            obj.apply(DeviceCommand("timebase", {"value": value}))
    assert peer.history == []


def test_slider_endpoint_uses_main_limit(page):
    page, app, key, peer = page
    slider = page.controls.sliders[1, "timebase"]
    assert max(slider.steps) == 0.02
    assert (
        app.sessions.capability_snapshot(key).channels[1].get("timebase").parameters["value"].maximum == 0.02
    )


def test_readonly_connection_never_recovers_roll(tmp_path):
    config = ConnectionConfig("mock://roll-readonly", "rigol.dho924", "mock", expected_serial="DHO-TEST")
    with ApplicationRuntime((config,), root=tmp_path, hardware=False) as app:
        app.monitoring.enable_settings_sync(False)
        peer = Peer()
        roll(peer)
        app.sessions.factory = lambda cfg: DHO924Adapter(peer, profile(), cfg)
        key = app.add(config)
        app.connect(key, configure_acquisition=False).result(5)
        app.sessions.submit(key, "refresh_capabilities").result(5)
        with pytest.raises(DeviceError, match="ROLL"):
            app.sessions.submit(key, "acquire", AcquisitionRequest("waveform")).result(5)
        assert all(command.endswith("?") for command in peer.history)
        assert peer.state[":TIM:MODE"] == "ROLL"


@pytest.mark.parametrize("entry", ["acquire", "refresh", "auto"])
def test_detected_roll_recovers_once_and_publishes_actual_settings(runtime, entry):
    app, key, peer = runtime
    app.monitoring.enable_settings_sync(False)
    if entry == "auto":
        prepare_auto(peer)
        original = peer.write

        def write(command):
            original(command)
            if command == ":AUToset":
                roll(peer)

        peer.write = write
        result = app.sessions.submit(key, "apply", DeviceCommand("auto_setup")).result(5)
        assert result.outcome == "success"
        assert peer.history.count(":AUToset") == 1
    else:
        roll(peer)
        if entry == "refresh":
            app.sessions.submit(key, "refresh_capabilities").result(5)
        else:
            app.sessions.submit(key, "acquire", AcquisitionRequest("waveform")).result(5)
    assert peer.history.count(":TIM:MAIN:SCAL 0.02") == 1
    assert peer.history.count(":TIM:MODE MAIN") == 1
    assert peer.state[":TRIG:STAT"] == "AUTO"
    reading = app.sessions.readback_frames(key)[1, "timebase"].reading
    assert reading.value == 0.02 and reading.source == "instrument"
    request = AcquisitionRequest("waveform", consistency="live", context_id="after-roll")
    app.sessions.submit(key, "acquire", request).result(5)
    peer.history.clear()
    assert app.sessions.submit(key, "acquire", request).result(5).validation == "cached"
    assert peer.history == [":WAV:PRE?", ":WAV:DATA?", ":WAV:PRE?"]


def test_roll_found_during_monitoring_resumes_without_new_start(runtime):
    app, key, peer = runtime
    app.monitoring.enable_settings_sync(False)
    roll(peer)
    app.monitoring.start(key, (AcquisitionRequest("waveform"),), interval=0.5)
    wait_for(lambda: app.monitoring.latest(key, "waveform") is not None)
    assert app.monitoring.snapshots()[key]["running"]
    assert app.monitoring.latest(key, "waveform").batch.configuration["1.timebase_mode"] == "MAIN"
    assert peer.history.count(":TIM:MODE MAIN") == 1


def test_cached_descriptor_change_discards_old_frame_then_recovers(runtime):
    app, key, peer = runtime
    app.monitoring.enable_settings_sync(False)
    request = AcquisitionRequest("waveform", consistency="live", context_id="external")
    first = app.sessions.submit(key, "acquire", request).result(5)
    roll(peer)
    with pytest.raises(DeviceError) as error:
        app.sessions.submit(key, "acquire", request).result(5)
    assert error.value.code == "configuration_changed"
    second = app.sessions.submit(key, "acquire", request).result(5)
    assert second.configuration["1.timebase"] == 0.02
    assert first.configuration["1.timebase"] == 1e-6


@pytest.mark.parametrize("failure", [":TIM:MAIN:SCAL 0.02", "*OPC?", ":TIM:MODE MAIN", ":TIM:MODE?"])
def test_uncertain_recovery_stops_without_retry(runtime, failure):
    app, key, peer = runtime
    app.monitoring.enable_settings_sync(False)
    roll(peer)
    # Snapshot queries must succeed; inject a failure only after the first write.
    original = peer.write

    def write(command):
        if command == ":TIM:MAIN:SCAL 0.02":
            peer.fail = failure
        original(command)

    peer.write = write
    request = AcquisitionRequest("waveform")
    with pytest.raises(DeviceError, match="未重复设置"):
        app.sessions.submit(key, "acquire", request).result(5)
    before = list(peer.history)
    with pytest.raises(DeviceError):
        app.sessions.submit(key, "acquire", request).result(5)
    assert peer.history == before
    assert peer.history.count(":TIM:MAIN:SCAL 0.02") == 1
    assert ":WAV:DATA?" not in peer.history
    assert app.sessions.snapshots()[0]["connectionState"] == "degraded"


def test_recovery_reservation_blocks_recording(runtime):
    app, key, peer = runtime
    app.monitoring.enable_settings_sync(False)
    roll(peer)
    entered, release = threading.Event(), threading.Event()
    original = peer.write

    def write(command):
        if command == ":TIM:MAIN:SCAL 0.02":
            entered.set()
            assert release.wait(4)
        original(command)

    peer.write = write
    app.monitoring.start(key, (AcquisitionRequest("waveform"),), interval=0.5)
    assert entered.wait(3)
    try:
        assert app.sessions.snapshots()[0]["controlPending"]
        with pytest.raises(DeviceError):
            app.monitoring.set_recording(key, True)
    finally:
        release.set()
    wait_for(lambda: app.monitoring.latest(key, "waveform") is not None)
    assert not app.sessions.snapshots()[0]["controlPending"]


def test_recording_lock_forbids_automatic_parameter_writes(runtime):
    app, key, peer = runtime
    app.monitoring.enable_settings_sync(False)
    roll(peer)
    session = app.sessions.sessions[key]
    session.recording = True
    peer.history.clear()
    try:
        with pytest.raises(DeviceError, match="停止记录"):
            app.sessions.submit(key, "acquire", AcquisitionRequest("waveform")).result(5)
        assert all(command.endswith("?") for command in peer.history)
        assert ":WAV:DATA?" not in peer.history
        assert session.state != "degraded"
    finally:
        session.recording = False


@pytest.mark.parametrize("fail", [False, True])
def test_hardware_entry_backups_and_retains_requested_state_offline(tmp_path, monkeypatch, fail):
    from ..hardware import main_mode

    peer = Peer()
    roll(peer)
    if fail:
        peer.fail = ":TIM:MAIN:SCAL 0.02"
    original_write = peer.write
    replies = []

    def write(command):
        if command.endswith("?"):
            replies.append(peer.query(command))
        else:
            original_write(command)

    peer.write = write
    peer.read = lambda: replies.pop(0)
    peer.query_timeout = lambda command, seconds: peer.query(command)
    peer.query_binary = lambda command, limit: peer.query(command)
    config = ConnectionConfig("mock://policy-hardware", "rigol.dho924", "mock", expected_serial="DHO-TEST")
    monkeypatch.setattr(main_mode, "verify_paths", lambda home: {"test": str(tmp_path)})
    monkeypatch.setattr(
        main_mode,
        "ApplicationRuntime",
        lambda configs: ApplicationRuntime(configs, root=tmp_path, hardware=False),
    )
    monkeypatch.setattr(main_mode, "create_adapter", lambda cfg, visa: DHO924Adapter(peer, profile(), cfg))
    monkeypatch.setattr(main_mode, "publish_report", lambda folder: None)
    monkeypatch.setattr(main_mode, "refresh_after_batch", lambda folder: None)
    folder = tmp_path / "evidence"
    folder.mkdir()
    report = main_mode.verify_main(config, folder)
    assert report["session_released"]
    assert report["initial_snapshot"]["values"]["1.timebase_mode"] == "ROLL"
    assert (folder / "results.json").is_file() and (folder / "communication.jsonl").is_file()
    assert peer.history.count(":TIM:MAIN:SCAL 0.02") == 1
    if fail:
        assert report["stages"]["parameter_write"] == "failed"
        assert report["stages"]["waveform"] == "not_tested"
    else:
        assert report["stages"]["transfer_restoration"] == "passed"
        assert report["final_snapshot"]["values"]["1.timebase_mode"] == "MAIN"
        assert report["final_snapshot"]["transfer"] == report["initial_snapshot"]["transfer"]
        assert len(report["frames"]) == 3
