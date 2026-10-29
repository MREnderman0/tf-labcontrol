from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

from ..hardware import readonly, verify
from ..hardware.records import summarize


def done(value=None):
    future = Future()
    future.set_result(value)
    return future


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
    )
    app = SimpleNamespace(
        monitoring=monitoring,
        sessions=SimpleNamespace(sessions={"device": SimpleNamespace(controls_locked=False)}),
    )
    report = {"stages": {}}
    test = verify.ThermistorTest(app, "device", tmp_path, report, 428)

    def frames(path):
        assert isinstance(path, Path)
        return [dict(measurements=[dict(channel=428, unit="°C")])] * 2

    monkeypatch.setattr(verify, "read_frames", frames)
    test.monitor(0)
    assert report["stages"]["recording"]["state"] == "passed"
    assert report["stages"]["stop"]["controls_unlocked"]


def test_failed_restoration_is_reported_and_other_fields_still_checked(tmp_path):
    test = verify.ThermistorTest(None, "device", tmp_path, {"operations": []}, 428)
    test.initial = {"scan_list": "(@)", "function": "VOLT"}
    test.latent = {"thermistor_type": 5000, "sensor": "TC", "unit": "C"}
    values = {
        "TEMP:TRAN:THER:TYPE? (@428)": "10000",
        "TEMP:TRAN:TYPE? (@428)": "THER",
        "UNIT:TEMP? (@428)": "C",
        "ROUT:SCAN?": "(@428)",
        "FUNC? (@428)": "TEMP",
        "SYST:ERR?": "0,No error",
    }
    restored = []

    def apply(operation, value, channel=1):
        restored.append(operation)
        if operation == "thermistor_type":
            raise RuntimeError("restoration timeout")
        query = {
            "temperature_sensor": "TEMP:TRAN:TYPE? (@428)",
            "scan_list": "ROUT:SCAN?",
            "function": "FUNC? (@428)",
        }[operation]
        values[query] = value

    test.apply = apply
    test.q = lambda query: values[query]
    test.snapshot = lambda: dict(test.initial)
    result = test.restore()
    assert result["state"] == "failed" and "timeout" in result["errors"][0]
    assert restored == ["thermistor_type", "temperature_sensor", "scan_list", "function"]
