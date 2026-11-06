import pytest

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import DeviceError
from lab_control.instruments.registry import build_scpi_adapter

from ..hardware.autoset import restore_transfer
from .fake import Peer


def test_setup_restore_separately_restores_and_reads_back_transport_format():
    peer = Peer()
    adapter = build_scpi_adapter(ConnectionConfig(peer.resource, "rs.rtm3004", "mock"), peer)
    initial = {"format": "CSV,0", "byte_order": "MSBF"}
    restore_transfer(adapter, initial)
    assert peer.history == [
        "FORM?",
        "FORM CSV,0",
        "FORM?",
        "FORM:BORD?",
        "FORM:BORD MSBF",
        "FORM:BORD?",
        "SYST:ERR?",
    ]
    peer.history.clear()
    restore_transfer(adapter, initial)
    assert peer.history == ["FORM?", "FORM:BORD?", "SYST:ERR?"]


def test_transfer_restore_mismatch_stops_without_retry_or_next_write(monkeypatch):
    peer = Peer()
    adapter = build_scpi_adapter(ConnectionConfig(peer.resource, "rs.rtm3004", "mock"), peer)
    original = peer.query
    monkeypatch.setattr(peer, "query", lambda q: original(q) if q != "FORM?" else b"REAL,32")
    with pytest.raises(DeviceError, match="Transfer restoration mismatch"):
        restore_transfer(adapter, {"format": "CSV,0", "byte_order": "MSBF"})
    assert peer.history == ["FORM CSV,0"]
