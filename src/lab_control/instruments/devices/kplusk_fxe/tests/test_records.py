from lab_control.instruments.devices.kplusk_fxe.hardware.records import summarize


def test_fxe2_summary_accepts_actual_identity_without_inventing_acceptance():
    result = summarize(
        {
            "identity": {
                "manufacturer": "K+K",
                "model": "FXE2",
                "serial": "TEST-517",
                "firmware": "E20-TEST",
                "confidence": "protocol",
            },
            "expected_serial": "TEST-517",
            "resource": "192.0.2.10",
            "started_at": "2026-10-30T00:00:00Z",
            "scope": "readonly discovery",
            "environment": {"library": "fixture"},
            "project_head": "fixture",
            "stages": {"identity": {"state": "passed"}, "physical_output": {"state": "not_tested"}},
        }
    )
    assert result["identity_verified"] and result["model"] == "K+K FXE2"
    assert result["interface"] == "LAN" and result["completeness"] == "complete"
    assert result["items"][1]["status"] == "not_tested"


def test_report_label_cannot_supply_missing_instrument_identity():
    result = summarize({"model": "K+K FXE2", "resource": "192.0.2.10", "stages": {"identity": "blocked"}})
    assert not result["identity_verified"] and result["completeness"] == "needs_review"
