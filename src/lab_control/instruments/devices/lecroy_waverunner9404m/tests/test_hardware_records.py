import pytest

from lab_control.domain.instruments import DeviceError
from lab_control.storage.hardware_records import validate

from ..hardware.records import summarize
from ..hardware.validate_control import AuditTransport
from .test_sessions import Scope


def test_backup_guard_blocks_all_settings(tmp_path):
    wire = Scope()
    transport = AuditTransport(wire, tmp_path, {"*IDN?"})
    with pytest.raises(DeviceError):
        transport.write("TRMD AUTO")
    assert wire.history == [] and transport.setting_writes == 0


def test_legacy_warning_is_not_promoted_to_parameter_verification():
    report = {
        "stages": {"parameter_read": "passed"},
        "operations": [{"query": "TDIV?", "reply": "WARNING : CURRENT REMOTE CONTROL INTERFACE IS TCPIP\n"}],
    }
    summary = summarize(report)
    assert not validate(summary)
    assert summary["items"][0]["status"] == "blocked"
    assert summary["items"][0]["raw_result"]["original_result"] == "passed"
    assert report["stages"]["parameter_read"] == "passed"


def test_missing_identity_and_failed_restore_remain_unverified():
    summary = summarize(
        {
            "stages": {"restoration": "failed", "parameter_write": "not_tested"},
            "final_state": "Unknown after connection loss",
        }
    )
    assert not summary["identity_verified"]
    assert summary["completeness"] == "needs_review"
    assert summary["items"][0]["status"] == "failed"
