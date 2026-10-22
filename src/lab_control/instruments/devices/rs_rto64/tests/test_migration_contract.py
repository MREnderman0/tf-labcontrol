import json
from pathlib import Path

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import jsonable
from lab_control.instruments.registry import describe


def test_migration_capabilities_preserve_original_contract():
    contract = json.loads(
        (Path(__file__).parent / "fixtures/capabilities-before.json").read_text(encoding="utf-8")
    )
    assert jsonable(describe(ConnectionConfig("metadata://test", "rs.rto64"))) == contract["capabilities"]
