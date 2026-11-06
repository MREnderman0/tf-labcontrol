import json
from pathlib import Path

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import jsonable
from lab_control.instruments.registry import describe


def test_current_contract_keeps_migration_evidence_separate():
    contract = json.loads(
        (Path(__file__).parent / "fixtures/capabilities-current.json").read_text(encoding="utf-8")
    )
    assert jsonable(describe(ConnectionConfig("metadata://test", "rs.rtm3004"))) == contract["capabilities"]
