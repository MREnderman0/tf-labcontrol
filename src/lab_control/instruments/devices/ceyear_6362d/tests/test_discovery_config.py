from dataclasses import replace

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import DeviceError
from lab_control.instruments.devices.ceyear_6362d.discovery import (
    normalize_discovery_config,
    select_source_address,
)

RESOURCE = "TCPIP0::192.168.5.241::10001::SOCKET"


def test_select_source_address_uses_a_deterministic_reachable_physical_source():
    calls = []

    def probe(host, port, source):
        calls.append((host, port, source))
        return source if source == "192.168.0.1" else None

    assert select_source_address(
        "192.168.5.241", 10001, ("198.18.0.1", "192.168.0.1"), probe
    ) == "192.168.0.1"
    assert calls == [("192.168.5.241", 10001, "192.168.0.1")]


def test_normalize_saved_visa_socket_to_tcp_with_selected_source(monkeypatch):
    monkeypatch.setattr(
        "lab_control.instruments.devices.ceyear_6362d.discovery.select_source_address",
        lambda *_args: "192.168.0.1",
    )
    config = ConnectionConfig(RESOURCE, "ceyear.6362d", timeout=7)
    normalized = normalize_discovery_config(config, ("192.168.0.1", "198.18.0.1"))
    assert normalized == replace(config, transport="tcp", source_address="192.168.0.1")


def test_normalize_preserves_an_explicit_source_address():
    config = ConnectionConfig(RESOURCE, "ceyear.6362d", transport="tcp", source_address="192.168.0.1")
    assert normalize_discovery_config(config, ()) == config


def test_normalize_reports_missing_physical_source():
    with pytest.raises(DeviceError, match="physical Ethernet") as error:
        normalize_discovery_config(ConnectionConfig(RESOURCE, "ceyear.6362d"), ())
    assert error.value.code == "source_address_required"


def test_normalize_leaves_other_resources_and_transports_unchanged():
    config = ConnectionConfig("TCPIP0::192.168.5.241::5025::SOCKET", "ceyear.6362d")
    assert normalize_discovery_config(config, ("192.168.0.1",)) == config
