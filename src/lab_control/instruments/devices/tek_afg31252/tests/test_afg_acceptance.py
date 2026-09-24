import runpy
from pathlib import Path

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.transport.base import MockTransport

SCRIPT = Path(__file__).parents[1] / "hardware/validate_usb.py"
MODULE = runpy.run_path(str(SCRIPT))


def guarded(tmp_path):
    transport = MockTransport([])
    config = ConnectionConfig(transport.resource, "tek.afg31252", expected_serial="TEST")
    return MODULE["GuardedTransport"](transport, tmp_path, config), transport


@pytest.mark.parametrize(
    "payload", ["OUTP1 ON", "*TRG", "*RST", "*CLS", "MMEM:DEL 'user'", "SOUR1:FREQ 1000;OUTP1 ON"]
)
def test_test_transport_never_allows_output_trigger_reset_or_storage(tmp_path, payload):
    guard, transport = guarded(tmp_path)
    guard.allow_settings = True
    with pytest.raises(RuntimeError):
        guard.write(payload)
    assert not transport.history


def test_incomplete_backup_blocks_all_setting_writes(tmp_path):
    guard, transport = guarded(tmp_path)
    with pytest.raises(RuntimeError, match="backups"):
        guard.write("SOUR1:FREQ 1000")
    assert not transport.history


def test_restore_failure_is_recorded_and_stops_dependent_steps():
    calls = []

    def failing():
        calls.append("failed")
        raise RuntimeError("device disconnected")

    result = MODULE["checked_restore"](
        [
            ("first", lambda: calls.append("first")),
            ("memory", failing),
            ("dependent", lambda: calls.append("should not run")),
        ]
    )
    assert calls == ["first", "failed"]
    assert result[-1] == {"step": "memory", "status": "failed", "error": "device disconnected"}


def test_snapshot_comparison_does_not_hide_missing_fields_or_changes():
    diff = MODULE["differences"](
        {"frequency": "1000", "output": "0", "mode": "CW"}, {"frequency": "1e3", "output": "1"}
    )
    assert set(diff) == {"output", "mode"}


@pytest.mark.parametrize(
    "raw,ch,expected",
    [
        ("EMEM", 1, True),
        ("EMEM", 2, True),
        ("EMEM2", 2, True),
        ("EMEM1", 2, False),
        ("SIN", 1, False),
        ("", 2, False),
    ],
)
def test_edit_selection_accepts_documented_generic_response(raw, ch, expected):
    assert MODULE["edit_selection_matches"](raw, ch) is expected


def test_report_sharing_violation_retries_only_local_replace(monkeypatch):
    class Temporary:
        calls = 0

        def replace(self, destination):
            self.calls += 1
            if self.calls == 1:
                raise PermissionError("Windows reader holding report")

    monkeypatch.setattr(MODULE["time"], "sleep", lambda delay: None)
    temporary = Temporary()
    MODULE["replace_report"](temporary, Path("results.json"))
    assert temporary.calls == 2


def test_permanent_report_permission_failure_is_not_hidden(monkeypatch):
    class Temporary:
        calls = 0

        def replace(self, destination):
            self.calls += 1
            raise PermissionError("Access denied")

    monkeypatch.setattr(MODULE["time"], "sleep", lambda delay: None)
    temporary = Temporary()
    with pytest.raises(PermissionError):
        MODULE["replace_report"](temporary, Path("results.json"))
    assert temporary.calls == 20


def test_merged_snapshot_reply_blocks_backup_before_setting_authorization():
    values = {f"OUTP{ch}?": "0" for ch in (1, 2)}
    for ch in (1, 2):
        values.update({f"OUTP{ch}:POL?": "NORM", f"SOUR{ch}:FUNC?": "SIN", f"SOUR{ch}:FREQ:MODE?": "CW"})
    MODULE["validate_snapshot"](values)
    values["OUTP2:POL?"] = "NORM0"
    with pytest.raises(RuntimeError, match="Invalid snapshot"):
        MODULE["validate_snapshot"](values)


def test_empty_timeout_is_still_a_failed_report(tmp_path, monkeypatch):
    runner = MODULE["Acceptance"](
        ConnectionConfig("USB::TEST", "tek.afg31252", expected_serial="TEST"), tmp_path
    )

    def timeout(*args, **kwargs):
        raise TimeoutError()

    monkeypatch.setitem(MODULE["Acceptance"].run.__globals__, "ApplicationRuntime", timeout)
    runner.run()
    assert runner.report["error"] == "TimeoutError"
    assert runner.report["error_type"] == "TimeoutError"
    assert runner.report["setting_write_count"] == 0
    assert runner.report["stages"]["basic"] == "not_tested"


def test_validation_only_probe_never_applies_an_unexpectedly_accepted_candidate(tmp_path):
    from lab_control.instruments.devices.tek_afg31252.hardware.validate_ranges import RangeAcceptance

    class Adapter:
        def validate_command(self, command):
            return None, command.parameters

        def preflight(self, command, values):
            return None

        def apply(self, command):
            pytest.fail("Invalid hardware candidate was sent")

    runner = RangeAcceptance(ConnectionConfig("USB::TEST", "tek.afg31252"), tmp_path)
    runner.worker = lambda fn: fn(Adapter())
    runner.outputs_off = lambda: None
    with pytest.raises(RuntimeError, match="unexpectedly accepted"):
        runner.rejected(1, "frequency", 250e6 + 1)
    assert runner.report["range_rejections"][0]["setting_writes"] == 0
