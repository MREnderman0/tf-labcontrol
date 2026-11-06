from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

from ..hardware import readonly, software_temperature, verify
from ..hardware.records import summarize


def done(value=None):
    future = Future()
    future.set_result(value)
    return future


def test_connection_setup_failure_is_recorded_without_git_or_hardware(monkeypatch, tmp_path):
    from lab_control.domain.connections import ConnectionConfig

    from ..hardware import connection_setup

    def unavailable(*_args, **_kwargs):
        raise FileNotFoundError("git is not installed")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Invalid profile must not open a runtime")

    monkeypatch.setattr(connection_setup.subprocess, "check_output", unavailable)
    monkeypatch.setattr(connection_setup, "ApplicationRuntime", forbidden)
    folder = tmp_path / "hardware-tests" / "RIGOL-M300-TEST" / "invalid-profile"
    result = connection_setup.commission(
        ConnectionConfig("mock://m300", "rigol.m300", "mock", expected_serial="TEST"),
        folder,
        root=tmp_path,
        channels={401: 1234},
    )
    assert result["error"] and result["model_source_hashes"]
    assert result["source_version"].startswith("Installed package")
    assert (folder / "results.json").is_file() and (folder / "summary.json").is_file()
    assert (folder / "record.md").is_file()


def test_record_converter_preserves_structured_revision_without_changing_original():
    report = {"source_version": {"commit": "example"}, "model_source_hashes": {"adapter.py": "digest"}}
    summary = summarize(report)
    assert summary["source_version"]["revision"] == report["source_version"]
    assert summary["source_version"]["model_source_hashes"] == report["model_source_hashes"]
    assert "project_head" not in report
    legacy = summarize(
        {"stages": {"restoration": {"state": "passed"}, "reconnection": {"state": "not_tested"}}}
    )
    assert {item["key"] for item in legacy["items"]} == {"recovery", "reconnect"}
    assert {item["source_key"] for item in legacy["items"]} == {"restoration", "reconnection"}


def test_readonly_entry_cannot_dispatch_control_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(readonly, "verify_main", lambda args, **kwargs: calls.append((args, kwargs)) or 0)
    assert readonly.main(["--resource", "example"]) == 0
    assert calls[0][1] == {"readonly": True}


def test_recording_verification_handles_public_segment_path_strings(monkeypatch, tmp_path):
    state = dict(error="", csv_error="", received=2, committed=2, converted=2)
    recorder = SimpleNamespace(
        segments=SimpleNamespace(paths=[str(tmp_path / "segment.sqlite3")]),
        finished=done(),
        snapshot=lambda: state,
    )
    monitoring = SimpleNamespace(
        start=lambda *a, **k: None,
        tasks={"device": SimpleNamespace(recorder=recorder)},
        stop=lambda *a: done(),
        temperature_window=lambda *a: (),
    )
    app = SimpleNamespace(
        monitoring=monitoring,
        sessions=SimpleNamespace(sessions={"device": SimpleNamespace(controls_locked=False)}),
    )
    report = {"stages": {}}
    test = software_temperature.SoftwareTemperatureTest(app, "device", tmp_path, report, {428: "4813-s02-v1"})

    def frames(path):
        assert isinstance(path, Path)
        return [
            dict(
                measurements=[
                    dict(
                        channel=428,
                        quantity="temperature",
                        unit="°C",
                        values=[24.99994974682602],
                        metadata={
                            "temperature_conversion": {"resistance_ohm": 10000, "curve_id": "4813-s02-v1"}
                        },
                    )
                ]
            )
        ] * 2

    monkeypatch.setattr(software_temperature, "read_frames", frames)
    test.monitor(0)
    assert report["stages"]["recording"]["state"] == "passed"
    assert report["stages"]["stop"]["controls_unlocked"]


def test_old_native_temperature_hardware_api_cannot_open_runtime(monkeypatch, tmp_path):
    from lab_control.domain.connections import ConnectionConfig

    def forbidden(*args, **kwargs):
        raise AssertionError("Retired temperature path must not access instruments")

    monkeypatch.setattr(verify, "ApplicationRuntime", forbidden)
    folder = tmp_path / "hardware-tests" / "RIGOL-M300-TEST" / "retired"
    report = verify.verify(
        ConnectionConfig("mock://m300", "rigol.m300", "mock", expected_serial="TEST"),
        folder,
        root=tmp_path,
        channel=428,
    )
    assert "已停用" in report["error"]
    assert (folder / "results.json").exists()


def test_curve_groups_are_explicit_and_reject_legacy_nominals():
    import pytest

    from lab_control.domain.instruments import DeviceError

    parse = software_temperature.parse_curve_groups
    assert len(parse(["401:416=mf501-v1", "417:419=4813-s02-v1"])) == 19
    for groups in (["401=5000"], ["401=mf501-v1", "401=4813-s02-v1"], ["499=mf501-v1"]):
        with pytest.raises((ValueError, DeviceError)):
            parse(groups)
