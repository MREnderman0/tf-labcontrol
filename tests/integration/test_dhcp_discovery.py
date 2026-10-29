from datetime import datetime
from types import SimpleNamespace

import pytest

from lab_control.application.leases import ResourceLeases
from lab_control.application.sessions import SessionManager
from lab_control.discovery.dhcp import DhcpLease, DhcpLeaseDiscoveryProvider, read_tftpd_leases
from lab_control.discovery.inventory import Inventory
from lab_control.discovery.lan import LanDiscoverySettings, RememberedLanDiscoveryProvider
from lab_control.discovery.presence import is_current
from lab_control.discovery.providers import ConfiguredDiscoveryProvider
from lab_control.discovery.service import DiscoveryService
from lab_control.discovery.types import DiscoveryCandidate
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import DeviceIdentity
from lab_control.storage.repository import Repository

MAC = "02:00:00:00:00:11"
SETTINGS = LanDiscoverySettings(enabled=True, networks=("192.0.2.0/24",))


@pytest.fixture
def store(tmp_path):
    repository = Repository(tmp_path)
    leases = ResourceLeases()
    sessions = SessionManager(leases, repository, lambda _: pytest.fail("No control initialization"))
    inventory = Inventory(repository)
    config = ConnectionConfig(
        "TCPIP0::192.0.2.10::5555::SOCKET",
        "rigol.dho924",
        expected_serial="TEST-A",
        timeout=7,
        limits={"fixture_limit": 0.5},
    )
    identity = DeviceIdentity("RIGOL TECHNOLOGIES", "DHO924", "TEST-A", "TEST")
    record = inventory.merge(DiscoveryCandidate(config, "configured"), "connected", identity, config.adapter)
    yield inventory, sessions, leases, record, identity
    sessions.close()
    repository.close()


def scan(store, providers, probe):
    inventory, sessions, leases, *_ = store
    scanner = DiscoveryService(tuple(providers), sessions, leases, inventory, probe)
    try:
        return scanner.scan().result(5)
    finally:
        scanner.close()


def set_leases(monkeypatch, address="192.0.2.10", mac=MAC):
    monkeypatch.setattr("lab_control.discovery.dhcp.local_dhcp_leases", lambda: (DhcpLease(mac, address),))


def test_current_lease_binds_only_after_real_identity_then_follows_new_ip_after_reload(store, monkeypatch):
    inventory, _, _, record, identity = store
    provider = DhcpLeaseDiscoveryProvider(inventory, SETTINGS)
    set_leases(monkeypatch)
    candidate = provider.enumerate()[0]
    assert (
        not candidate.config.adapter and not candidate.config.expected_serial and not candidate.config.limits
    )
    assert "dhcpBinding" not in record
    scan(store, [provider], lambda _: (identity, "rigol.dho924"))
    assert record["dhcpBinding"] == {"mac": MAC, "identityKey": identity.key}
    inventory.records = Inventory(inventory.repository).records
    set_leases(monkeypatch, "192.0.2.42")
    candidates = provider.enumerate()
    assert [c.config.resource for c in candidates] == ["TCPIP0::192.0.2.42::5555::SOCKET"]
    assert candidates[0].config.timeout == 7
    scan(store, [provider], lambda _: (identity, "rigol.dho924"))
    current = inventory.all()[0]
    assert current["id"] == record["id"] and len(inventory.all()) == 1
    assert current["config"]["resource"] == candidates[0].config.resource
    assert current["config"]["limits"] == {"fixture_limit": 0.5}
    assert current["config"]["expected_serial"] == "TEST-A" and is_current(current)
    stale = ConnectionConfig("TCPIP0::192.0.2.10::5555::SOCKET", "rigol.dho924", expected_serial="TEST-A")
    assert ConfiguredDiscoveryProvider((stale,), inventory).enumerate() == ()
    assert RememberedLanDiscoveryProvider(inventory, SETTINGS).enumerate() == ()
    # Manual pinning explicitly ends following and re-enables remembered probing.
    inventory.select_resource(current["id"], current["config"]["resource"])
    assert DhcpLeaseDiscoveryProvider(inventory, SETTINGS).enumerate() == ()
    assert (
        RememberedLanDiscoveryProvider(inventory, SETTINGS).enumerate()[0].config.resource
        == current["config"]["resource"]
    )


def test_dhcp_identity_guard_survives_deduplication_and_wrong_device_cannot_rebind(store, monkeypatch):
    inventory, _, _, record, _ = store
    set_leases(monkeypatch)
    provider = DhcpLeaseDiscoveryProvider(inventory, SETTINGS)
    configured = SimpleNamespace(
        name="configured",
        enumerate=lambda: (DiscoveryCandidate(ConnectionConfig(**record["config"]), "configured"),),
    )
    wrong = DeviceIdentity("RIGOL TECHNOLOGIES", "DHO924", "OTHER", "TEST")
    calls = []
    result = scan(
        store,
        [configured, RememberedLanDiscoveryProvider(inventory, SETTINGS), provider],
        lambda c: (calls.append(c), (wrong, "rigol.dho924"))[1],
    )
    assert len(calls) == len(result["items"]) == 1
    assert result["items"][0]["errorCode"] == "identity_conflict"
    assert "dhcpBinding" not in record and record["identity"]["serial"] == "TEST-A"
    assert not is_current(record)


@pytest.mark.parametrize(
    "condition", ["fixed", "conflict", "retired", "wrong_mac", "foreign_network", "unknown_ip"]
)
def test_no_speculative_or_unapproved_targets(store, monkeypatch, condition):
    inventory, _, _, record, identity = store
    set_leases(monkeypatch)
    if condition == "fixed":
        record["primaryResourceExplicit"] = True
    if condition == "conflict":
        record["identityConflict"] = {"reason": "test"}
    if condition == "retired":
        record["candidates"][record["config"]["resource"]]["retired"] = True
    if condition == "wrong_mac":
        record["dhcpBinding"] = {"identityKey": identity.key, "mac": "02:00:00:00:00:22"}
    if condition in {"foreign_network", "unknown_ip"}:
        set_leases(monkeypatch, "198.51.100.42" if condition == "foreign_network" else "192.0.2.42")
    assert DhcpLeaseDiscoveryProvider(inventory, SETTINGS).enumerate() == ()


def test_disabled_unplugged_and_active_session_are_respected(store, monkeypatch):
    inventory, sessions, _, record, identity = store
    set_leases(monkeypatch)
    assert DhcpLeaseDiscoveryProvider(inventory, LanDiscoverySettings()).enumerate() == ()
    monkeypatch.setattr("lab_control.discovery.lan.ethernet_networks", lambda: ())
    assert (
        DhcpLeaseDiscoveryProvider(
            inventory, LanDiscoverySettings(enabled=True, auto_ethernet=True)
        ).enumerate()
        == ()
    )
    sessions.sessions["another-session"] = object()
    try:
        result = scan(
            store,
            [DhcpLeaseDiscoveryProvider(inventory, SETTINGS)],
            lambda _: pytest.fail("Active alias gate"),
        )
        assert result["items"][0]["reason"] == "deferred_possible_alias_of_active_device"
        assert "dhcpBinding" not in record
    finally:
        sessions.sessions.clear()


def write_leases(tmp_path, entries):
    text = "[DHCP]\nLease (minutes)=60\nLease_NumLeases=" + str(len(entries)) + "\n"
    for i, (mac, ip, started) in enumerate(entries):
        text += f"Lease_{i}_MAC={mac}\nLease_{i}_IP={ip}\nLease_{i}_LeaseStartTime={started}\n"
    path = tmp_path / "leases.ini"
    path.write_text(text, encoding="utf-8-sig")
    return path


def test_lease_reader_rejects_offer_only_expired_future_and_duplicate_clients(tmp_path):
    entries = [
        (MAC, "192.0.2.10", "10/26/2026/10:30:00"),
        ("02:00:00:00:00:22", "192.0.2.11", "12/31/1969/16:00:00"),
        ("02:00:00:00:00:33", "192.0.2.12", "10/26/2026/08:00:00"),
        ("02:00:00:00:00:44", "192.0.2.13", "10/26/2026/12:00:00"),
        ("02:00:00:00:00:55", "192.0.2.14", "10/26/2026/10:30:00"),
        ("02:00:00:00:00:66", "192.0.2.14", "10/26/2026/10:30:00"),
        ("02:00:00:00:00:77", "192.0.2.15", "10/26/2026/10:30:00"),
        ("02:00:00:00:00:77", "192.0.2.16", "10/26/2026/10:30:00"),
    ]
    path = write_leases(tmp_path, entries)
    assert read_tftpd_leases(path, now=datetime(2026, 10, 26, 11)) == (DhcpLease(MAC, "192.0.2.10"),)
    path.write_text("[DHCP]\nLease_NumLeases=999999\nLease (minutes)=60\n", encoding="utf-8")
    with pytest.raises(Exception, match="count"):
        read_tftpd_leases(path)
