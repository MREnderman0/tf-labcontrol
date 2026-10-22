"""Model-owned identity admission is separate from the protocol used to identify."""

import ctypes
from dataclasses import replace

import pytest

from lab_control.application.leases import ResourceLeases
from lab_control.application.sessions import SessionManager
from lab_control.discovery.service import ConfiguredDiscoveryProvider, DiscoveryService, Inventory
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import DeviceIdentity
from lab_control.instruments import registry
from lab_control.storage.repository import Repository


@pytest.fixture
def scan(tmp_path):
    repository = Repository(tmp_path)
    leases = ResourceLeases()
    sessions = SessionManager(leases, repository, lambda _: pytest.fail("Scan opened a session factory"))
    inventory = Inventory(repository)
    scanners = []

    def run(config, probe):
        scanner = DiscoveryService(
            (ConfiguredDiscoveryProvider((config,)),), sessions, leases, inventory, probe
        )
        scanners.append(scanner)
        result = scanner.scan().result(timeout=3)
        assert not sessions.sessions
        return result

    yield run
    for scanner in scanners:
        scanner.close()
    sessions.close()
    repository.close()


@pytest.mark.parametrize(
    ("adapter_id", "transport"),
    [
        ("pico.6404e", "pico"),
        ("thorlabs.pm100d", "tlpmx"),
        ("thorlabs.pm100d", "pm100d"),
        ("microchip.53100a", "tserve"),
    ],
)
def test_configured_scan_defers_identity_without_loading_sdk_or_writing(
    scan, monkeypatch, adapter_id, transport
):
    def forbidden(*args, **kwargs):
        pytest.fail("Deferred discovery must not load a driver, construct an adapter or send commands")

    monkeypatch.setattr(ctypes, "CDLL", forbidden)
    monkeypatch.setattr(ctypes, "WinDLL", forbidden, raising=False)
    monkeypatch.setattr(registry, "create_adapter", forbidden)
    config = ConnectionConfig("metadata://selected-device", adapter_id, transport)
    report = scan(config, forbidden)
    assert report["items"][0]["reason"] == "sdk_identity_requires_explicit_connection"
    assert not report["items"][0]["error"]


def test_policy_comes_from_definition_instead_of_transport_name(scan, monkeypatch):
    definition = registry.DEVICE_DEFINITIONS["pico.6404e"]
    monkeypatch.setitem(
        registry.DEVICE_DEFINITIONS,
        definition.adapter_id,
        replace(definition, discovery_connection_only_transports=("serial",)),
    )
    report = scan(
        ConnectionConfig("COM-SYNTHETIC", definition.adapter_id, "serial"),
        lambda _: pytest.fail("Model-owned serial deferral was ignored"),
    )
    assert report["items"][0]["reason"] == "sdk_identity_requires_explicit_connection"


def test_microchip_non_tserve_transport_keeps_existing_probe_admission(scan):
    calls = []

    def probe(candidate):
        calls.append(candidate.config)
        return DeviceIdentity("Microchip", "53100A", "SYNTHETIC"), "microchip.53100a"

    config = ConnectionConfig("USB::SYNTHETIC", "microchip.53100a", "visa")
    report = scan(config, probe)
    assert calls == [config]
    assert report["items"][0]["reason"] == "identified"
    # This checks discovery routing, not a claim that the model supports VISA.


def test_coremorrow_adapter_identity_still_probes_with_read_queries_only(scan, monkeypatch):
    from lab_control.instruments.devices.coremorrow_xmt import DEFINITION
    from lab_control.instruments.devices.coremorrow_xmt.adapter import CoreMorrowXmtAdapter
    from lab_control.instruments.devices.coremorrow_xmt.protocol import READ_POWER_CONFIG_COMMAND
    from lab_control.instruments.devices.coremorrow_xmt.tests.fakes import WirePeer

    peer = WirePeer()
    monkeypatch.setattr(peer, "write_frame", lambda _: pytest.fail("Identity scan wrote a setting"))
    calls = []

    def probe(candidate):
        calls.append(candidate.config)
        adapter = CoreMorrowXmtAdapter(peer, DEFINITION.profile, candidate.config)
        return adapter.identify(), DEFINITION.adapter_id

    config = ConnectionConfig("COM-SYNTHETIC", DEFINITION.adapter_id, "serial")
    report = scan(config, probe)
    assert DEFINITION.identity_requires_adapter and not DEFINITION.discovery_connection_only_transports
    assert calls == [config]
    assert report["items"][0]["reason"] == "identified"
    assert len(peer.history) == 1 and peer.history[0][3] == READ_POWER_CONFIG_COMMAND
