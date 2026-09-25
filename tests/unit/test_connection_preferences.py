from types import SimpleNamespace

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.connection_preferences import preference_key, qualified_connection
from lab_control.instruments.devices.rigol_dsa815.adapter import Dsa815Adapter
from lab_control.instruments.devices.rigol_dsa815.hardware.benchmark import qualifies
from lab_control.storage.repository import Repository


def test_qualified_socket_follows_new_host_without_copying_verification(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "lab_control.application.connection_preferences.environment_signature", lambda _: {"visa": "test"}
    )
    repo = Repository(tmp_path)
    try:
        record = {"identity": {"serial": "REDACTED", "firmware": "fixture"}}
        config = ConnectionConfig("TCPIP0::192.0.2.7::inst0::INSTR", "rigol.dsa815")
        qualified = {
            "passed": True,
            "serial": "REDACTED",
            "firmware": "fixture",
            "port": 5555,
            "adapter_revision": Dsa815Adapter.acquisition_revision,
            "environment": {"visa": "test"},
        }
        repo.save_workspace(preference_key(config.adapter, "REDACTED"), qualified)
        result, firmware = qualified_connection(record, config, repo, SimpleNamespace())
        assert result.resource == "TCPIP0::192.0.2.7::5555::SOCKET"
        assert firmware == "fixture" and result.expected_serial == "REDACTED"
        assert config.resource in result.aliases
        assert record == {"identity": {"serial": "REDACTED", "firmware": "fixture"}}
        record["primaryResourceExplicit"] = True
        assert qualified_connection(record, config, repo, None) == (config, "")
        record.pop("primaryResourceExplicit")
        for key in ("environment", "firmware", "adapter_revision", "passed"):
            repo.save_workspace(preference_key(config.adapter, "REDACTED"), {**qualified, key: "changed"})
            assert qualified_connection(record, config, repo, None) == (config, "")
    finally:
        repo.close()


@pytest.mark.parametrize("missing", [True, False])
def test_performance_gate_requires_complete_evidence(missing):
    stages = {"connection": "passed", "backup": "passed", "physical_output": "not_tested"}
    stages.update(
        {
            f"{transport}_{group}": "passed"
            for transport in ("vxi11", "socket")
            for group in (
                "setup",
                "ASC_full",
                "ASC_live",
                "REAL_full",
                "REAL_live",
                "zero_span_full",
                "zero_span_live",
                "auto",
                "restoration",
                "reconnection",
            )
        }
    )
    if missing:
        stages.pop("socket_restoration")
    baseline = {"frames": 20, "median": 10, "p95": 11}
    assert qualifies(baseline, {"frames": 20, "median": 8, "p95": 10}, stages) is not missing
    assert not qualifies(baseline, {"frames": 20, "median": 8.1, "p95": 10}, stages)
    assert not qualifies(baseline, {"frames": 20, "median": 7, "p95": 12}, stages)
    assert not qualifies(
        baseline,
        {"frames": 20, "median": 7, "p95": 10},
        stages,
        production_strategy_differs=True,
    )
