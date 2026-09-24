from ..hardware.records import summarize


def test_summary_keeps_failed_reconnect_and_unmeasured_physical_output_separate():
    report = {
        "identity": {
            "manufacturer": "GW INSTEK",
            "model": "GPD-4303S",
            "serial": "SN:TEST",
            "firmware": "V2.12",
        },
        "expected_serial": "SN:TEST",
        "resource": "mock://gpd",
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:01:00Z",
        "scope": "Fixture only",
        "environment": {"backend": "fixture"},
        "source_sha256": {"test": "fixture"},
        "stages": {"restoration": "passed", "local_reconnect": "blocked", "physical_output": "not_tested"},
        "final_output_state": False,
        "error": "timeout after LOCAL",
    }
    summary = summarize(report)
    assert {item["key"]: item["status"] for item in summary["items"]} == report["stages"]
    assert summary["final_state"]["final_output_state"] is False
    assert summary["diagnostics"]["error"] == report["error"]
