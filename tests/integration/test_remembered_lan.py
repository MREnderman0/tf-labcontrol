from types import SimpleNamespace

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.leases import ResourceLeases
from lab_control.application.sessions import SessionManager
from lab_control.discovery.lan import LanDiscoverySettings, RememberedLanDiscoveryProvider
from lab_control.discovery.presence import is_current
from lab_control.discovery.service import DiscoveryCandidate, DiscoveryService, Inventory
from lab_control.domain.instruments import DeviceIdentity
from lab_control.storage.repository import Repository


@pytest.fixture
def store(tmp_path):
    repository = Repository(tmp_path)
    leases = ResourceLeases()
    sessions = SessionManager(leases, repository, lambda _: pytest.fail("Scan must not open control session"))
    inventory = Inventory(repository)
    yield inventory, sessions, leases
    sessions.close()
    repository.close()


def saved(inventory, host="192.0.2.10", serial="FIXTURE-A", suffix="5025::SOCKET"):
    config = ConnectionConfig(
        f"TCPIP0::{host}::{suffix}",
        "rigol.dg5504pro",
        expected_serial=serial,
        timeout=7,
        limits={"amplitude": 0.5},
    )
    identity = DeviceIdentity("RIGOL", "DG5504 Pro", serial, "TEST")
    record = inventory.merge(DiscoveryCandidate(config, "configured"), "old", identity, config.adapter)
    return record, identity


def provider(inventory):
    return RememberedLanDiscoveryProvider(
        inventory, LanDiscoverySettings(enabled=True, networks=("192.0.2.0/24",))
    )


def scan(store, providers, probe):
    inventory, sessions, leases = store
    scanner = DiscoveryService(tuple(providers), sessions, leases, inventory, probe)
    try:
        return scanner.scan().result(5)
    finally:
        scanner.close()


def test_socket_missing_from_broadcast_is_rechecked_after_restart(store):
    inventory, _, _ = store
    record, identity = saved(inventory)
    inventory.edit(record["id"], display_name="Saved source", slot="signal_generator:1")
    restarted = Inventory(inventory.repository)
    assert not is_current(restarted.all()[0])
    items = provider(restarted).enumerate()
    assert len(items) == 1 and items[0].source == "remembered_lan" and items[0].safe_scpi
    assert items[0].config.timeout == 7
    assert not items[0].config.adapter and not items[0].config.expected_serial
    assert not items[0].config.limits and not items[0].config.aliases
    calls = []
    scan(
        (restarted, *store[1:]),
        [provider(restarted)],
        lambda c: (calls.append(c.config.resource), (identity, "rigol.dg5504pro"))[1],
    )
    assert calls == [record["config"]["resource"]]
    current = restarted.all()[0]
    assert is_current(current) and current["id"] == record["id"]
    assert current["displayName"] == "Saved source" and current["config"]["limits"] == {"amplitude": 0.5}


def test_unresponsive_saved_primary_does_not_claim_current_presence(store):
    inventory, _, _ = store
    record, _ = saved(inventory)

    def absent(candidate):
        raise TimeoutError("Fixture unplugged")

    report = scan(store, [provider(inventory)], absent)
    assert len(report["items"]) == 1 and report["items"][0]["reason"] == "probe_failed"
    assert not is_current(inventory.all()[0])
    assert inventory.all()[0]["id"] == record["id"]


def test_address_following_uses_latest_verified_primary_not_provider_creation_snapshot(store):
    inventory, _, _ = store
    record, identity = saved(inventory)
    remembered = provider(inventory)
    old_resource = record["config"]["resource"]
    assert remembered.enumerate()[0].config.resource == old_resource
    new_resource = "TCPIP0::192.0.2.25::5025::SOCKET"
    inventory.merge(
        DiscoveryCandidate(ConnectionConfig(new_resource), "lan", safe_scpi=True),
        "new-address",
        identity,
        "rigol.dg5504pro",
        reason="identified",
    )
    assert [c.config.resource for c in remembered.enumerate()] == [new_resource]
    current = inventory.all()[0]
    assert current["id"] == record["id"] and old_resource in current["resources"]
    assert current["config"]["timeout"] == 7 and current["config"]["limits"] == {"amplitude": 0.5}


def test_reassigned_address_does_not_transfer_old_asset_or_limits(store):
    inventory, _, _ = store
    record, _ = saved(inventory)
    other = DeviceIdentity("RIGOL", "DG5504 Pro", "FIXTURE-B", "TEST")
    scan(store, [provider(inventory)], lambda _: (other, "rigol.dg5504pro"))
    old = next(r for r in inventory.all() if r["id"] == record["id"])
    new = next(r for r in inventory.all() if r["identity"]["serial"] == "FIXTURE-B")
    assert not is_current(old) and is_current(new)
    assert old["candidates"][record["config"]["resource"]]["retired"]
    assert new["config"]["limits"] == {} and new["config"]["expected_serial"] == "FIXTURE-B"


@pytest.mark.parametrize("kind", ["retired", "unverified", "conflict", "foreign_network", "usb"])
def test_only_verified_nonconflicting_primary_on_lab_network_is_probed(store, kind):
    inventory, _, _ = store
    record, _ = saved(inventory, host="198.51.100.10" if kind == "foreign_network" else "192.0.2.10")
    entry = record["candidates"][record["config"]["resource"]]
    if kind == "retired":
        entry["retired"] = True
    if kind == "unverified":
        entry.pop("verifiedIdentityKey")
    if kind == "conflict":
        record["identityConflict"] = {"reason": "fixture"}
    if kind == "usb":
        record["config"]["resource"] = "USB0::fixture::INSTR"
    assert provider(inventory).enumerate() == ()


def test_disabled_or_unplugged_ethernet_does_not_probe_and_new_scope_is_used(store, monkeypatch):
    inventory, _, _ = store
    saved(inventory)
    assert RememberedLanDiscoveryProvider(inventory, LanDiscoverySettings()).enumerate() == ()
    scopes = iter([(), ("198.51.100.0/24",), ("192.0.2.0/24",)])
    monkeypatch.setattr("lab_control.discovery.lan.ethernet_networks", lambda: next(scopes))
    remembered = RememberedLanDiscoveryProvider(
        inventory, LanDiscoverySettings(enabled=True, auto_ethernet=True)
    )
    assert remembered.enumerate() == ()
    assert remembered.enumerate() == ()
    assert len(remembered.enumerate()) == 1


def test_broadcast_and_saved_resource_deduplicate_and_active_session_defers(store):
    inventory, sessions, _ = store
    record, identity = saved(inventory, suffix="inst0::INSTR")
    announcement = DiscoveryCandidate(ConnectionConfig(record["config"]["resource"]), "lan", safe_scpi=True)
    broadcast = SimpleNamespace(name="lan", enumerate=lambda: (announcement,))
    calls = []
    result = scan(
        store, [broadcast, provider(inventory)], lambda c: (calls.append(c), (identity, "rigol.dg5504pro"))[1]
    )
    assert len(calls) == len(result["items"]) == 1
    sessions.sessions["active-fixture"] = object()
    try:
        result = scan(store, [provider(inventory)], lambda _: pytest.fail("Possible active alias"))
        assert result["items"][0]["reason"] == "deferred_possible_alias_of_active_device"
    finally:
        sessions.sessions.clear()
