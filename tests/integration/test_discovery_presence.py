import threading
from dataclasses import replace

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.leases import ResourceLeases
from lab_control.application.sessions import SessionManager
from lab_control.demo.instrument import SimulatedInstrument
from lab_control.discovery.service import (
    ConfiguredDiscoveryProvider,
    DiscoveryCandidate,
    DiscoveryService,
    Inventory,
)
from lab_control.domain.instruments import DeviceError, DeviceIdentity
from lab_control.storage.repository import Repository


class Provider:
    name = "visa"

    def __init__(self, *candidates, failed=False):
        self.candidates, self.failed = candidates, failed

    def enumerate(self):
        if self.failed:
            raise RuntimeError("fixture provider unavailable")
        return self.candidates


@pytest.fixture
def store(tmp_path):
    repo = Repository(tmp_path)
    leases = ResourceLeases()
    sessions = SessionManager(leases, repo, SimulatedInstrument)
    inventory = Inventory(repo)
    scanners = []

    def scan(providers, probe):
        scanner = DiscoveryService(tuple(providers), sessions, leases, inventory, probe)
        scanners.append(scanner)
        return scanner.scan().result(timeout=5)

    yield repo, inventory, scan
    for scanner in scanners:
        scanner.close()
    sessions.close()
    repo.close()


def absent(_candidate):
    raise TimeoutError("fixture: instrument unplugged")


@pytest.mark.parametrize(
    ("code", "driver_status"),
    [("connection_failed", "unconfirmed"), ("timeout", "unconfirmed"), ("driver_missing", "error")],
)
def test_probe_connection_failure_is_not_a_driver_failure(store, code, driver_status):
    _repo, inventory, scan = store
    candidate = DiscoveryCandidate(ConnectionConfig("TCPIP0::192.0.2.10::INSTR"), "lan", safe_scpi=True)

    def failed_probe(_candidate):
        raise DeviceError("fixture probe error", code)

    result = scan([Provider(candidate)], failed_probe)
    record = inventory.all()[0]
    assert record["presenceState"] == "unreachable"
    assert record["driverStatus"] == driver_status
    assert record["probeErrorCode"] == code
    assert result["items"][0]["errorCode"] == code


def test_absent_configured_sma_keeps_history_without_current_presence(store):
    repo, inventory, scan = store
    config = ConnectionConfig("USB::fixture::INSTR", "rs.sma100b")
    identity = DeviceIdentity("Rohde&Schwarz", "SMA100B", "FIXTURE")
    record = inventory.merge(DiscoveryCandidate(config, "visa"), "old", identity, config.adapter)
    key, last_seen = record["id"], record["lastSeenAt"]
    inventory.edit(key, display_name="Saved RF source", slot="signal_generator:1")
    scan([ConfiguredDiscoveryProvider((config,)), Provider()], absent)
    record = inventory.all()[0]
    assert record["id"] == key and record["identity"]["model"] == "SMA100B"
    assert record["displayName"] == "Saved RF source" and record["slots"]
    assert record["presenceState"] == "unreachable"
    assert record["discoveryState"] != "identified"
    assert record["lastSeenAt"] == last_seen
    assert set(record["candidates"][config.resource]["sources"]) == {"visa", "configured"}
    assert Inventory(repo).all()[0]["presenceState"] == "unknown"


def test_local_enumeration_and_config_keep_both_sources_and_one_probe(store):
    _repo, inventory, scan = store
    config = ConnectionConfig("USB::fixture::INSTR", "rs.sma100b", timeout=7)
    candidate = DiscoveryCandidate(replace(config, adapter=""), "visa", safe_scpi=True)
    probes = []

    def probe(c):
        probes.append(c)
        return DeviceIdentity("Rohde&Schwarz", "SMA100B", "FIXTURE"), config.adapter

    scan([Provider(candidate), ConfiguredDiscoveryProvider((config,))], probe)
    record = inventory.all()[0]
    assert len(probes) == 1 and probes[0].config.timeout == 7
    assert record["presenceState"] == "present"
    assert set(record["candidates"][config.resource]["sources"]) == {"visa", "configured"}


@pytest.mark.parametrize("explicit_timeout", [None, 7])
@pytest.mark.parametrize("replacement", [False, True])
def test_known_lan_probe_uses_saved_timeout_but_rechecks_identity(store, explicit_timeout, replacement):
    _repo, inventory, scan = store
    saved = ConnectionConfig(
        "TCPIP0::192.0.2.10::inst0::INSTR",
        "rigol.dsa815",
        timeout=15,
        expected_serial="SAVED",
        limits={"reference_level": 0},
    )
    old_identity = DeviceIdentity("RIGOL", "DSA815", "SAVED")
    old = inventory.merge(
        DiscoveryCandidate(saved, "lan", safe_scpi=True),
        "old",
        old_identity,
        saved.adapter,
        reason="identified",
    )
    discovered = DiscoveryCandidate(ConnectionConfig(saved.resource), "lan", safe_scpi=True)
    providers = [Provider(discovered)]
    if explicit_timeout is not None:
        providers.append(
            ConfiguredDiscoveryProvider((ConnectionConfig(saved.resource, timeout=explicit_timeout),))
        )
    probes = []
    found_identity = DeviceIdentity("RIGOL", "DSA815", "NEW") if replacement else old_identity

    def probe(candidate):
        probes.append(candidate.config)
        if candidate.config.timeout != (explicit_timeout or 15):
            raise TimeoutError("Discovery discarded the saved connection timeout")
        return found_identity, saved.adapter

    report = scan(providers, probe)
    assert len(probes) == 1
    # Historical identity, limits and model commands must not be applied to an unverified address.
    assert probes[0].adapter == "" and probes[0].expected_serial == "" and probes[0].limits == {}
    assert report["items"][0]["reason"] == "identified"
    assert report["items"][0]["probeTimeoutSeconds"] == (explicit_timeout or 15)
    current = inventory.by_resource(saved.resource)
    assert current["presenceState"] == "present"
    assert (current["id"] != old["id"]) == replacement
    if replacement:
        assert current["config"]["limits"] == {}
    else:
        assert current["config"]["timeout"] == 15


def test_metadata_network_address_is_not_a_current_device(store):
    _repo, inventory, scan = store
    candidate = DiscoveryCandidate(ConnectionConfig("TCPIP0::example.invalid::inst0::INSTR"), "visa")
    scan([Provider(candidate)], lambda _: pytest.fail("Unknown network address must not be probed"))
    assert inventory.all()[0]["presenceState"] == "unknown"
    assert not inventory.all()[0].get("lastSeenAt")


@pytest.mark.parametrize("failed", [False, True])
def test_missing_local_resource_distinguishes_absence_from_provider_failure(store, failed):
    _repo, inventory, scan = store
    candidate = DiscoveryCandidate(ConnectionConfig("USB::fixture::INSTR"), "visa")
    inventory.merge(candidate, "old")
    scan([Provider(failed=failed)], absent)
    record = inventory.all()[0]
    assert record["presenceState"] == ("unknown" if failed else "not_seen")
    assert (record["discoveryState"] == "stale") is (not failed)


def test_one_present_alias_and_rediscovery_preserve_identity_and_id(store):
    _repo, inventory, scan = store
    usb = ConnectionConfig("USB::fixture::INSTR", "rs.sma100b")
    lan = ConnectionConfig("TCPIP0::example.invalid::inst0::INSTR", "rs.sma100b")
    identity = DeviceIdentity("Rohde&Schwarz", "SMA100B", "FIXTURE")
    key = inventory.merge(DiscoveryCandidate(usb, "visa"), "old", identity, usb.adapter)["id"]
    inventory.merge(DiscoveryCandidate(lan, "configured"), "old", identity, lan.adapter)

    def probe(candidate):
        if candidate.config.resource == lan.resource:
            return identity, lan.adapter
        return absent(candidate)

    scan([ConfiguredDiscoveryProvider((usb, lan)), Provider()], probe)
    assert len(inventory.all()) == 1
    assert inventory.all()[0]["id"] == key
    assert inventory.all()[0]["presenceState"] == "present"
    scan([ConfiguredDiscoveryProvider((usb, lan)), Provider()], absent)
    assert inventory.all()[0]["presenceState"] == "unreachable"
    scan([ConfiguredDiscoveryProvider((lan,))], probe)
    assert inventory.all()[0]["id"] == key
    assert inventory.all()[0]["presenceState"] == "present"


def test_cancelled_scan_does_not_reuse_previous_presence_or_mark_missing(tmp_path):
    repo = Repository(tmp_path)
    inventory = Inventory(repo)
    leases = ResourceLeases()
    sessions = SessionManager(leases, repo, SimulatedInstrument)
    configs = tuple(ConnectionConfig(f"mock://{i}", "rs.sma100b", "mock") for i in range(2))
    for i, config in enumerate(configs):
        inventory.merge(
            DiscoveryCandidate(config, "configured"),
            "old",
            DeviceIdentity("Rohde&Schwarz", "SMA100B", f"FIXTURE-{i}"),
            config.adapter,
        )
    entered, release = threading.Event(), threading.Event()

    def probe(candidate):
        entered.set()
        assert release.wait(3)
        return DeviceIdentity("Rohde&Schwarz", "SMA100B", "FIXTURE-0"), candidate.config.adapter

    scanner = DiscoveryService((ConfiguredDiscoveryProvider(configs),), sessions, leases, inventory, probe)
    try:
        future = scanner.scan()
        assert entered.wait(3)
        scanner.cancel()
        release.set()
        assert future.result(timeout=3)["state"] == "cancelled"
        states = {r["identity"]["serial"]: r for r in inventory.all()}
        assert states["FIXTURE-0"]["presenceState"] == "present"
        assert states["FIXTURE-1"]["presenceState"] == "unknown"
        assert states["FIXTURE-1"]["discoveryState"] != "stale"
    finally:
        release.set()
        scanner.close()
        sessions.close()
        repo.close()


def test_failed_active_session_is_not_promoted_by_identity_cache(tmp_path):
    repo = Repository(tmp_path)
    inventory = Inventory(repo)
    leases = ResourceLeases()
    sessions = SessionManager(leases, repo, SimulatedInstrument)
    config = ConnectionConfig("mock://session", "rs.sma100b", "mock")
    identity = sessions.connect("active", config).result(timeout=3)
    inventory.merge(DiscoveryCandidate(config, "configured"), "old", identity, config.adapter)
    scanner = DiscoveryService(
        (ConfiguredDiscoveryProvider((config,)),),
        sessions,
        leases,
        inventory,
        lambda _: pytest.fail("An owned resource must never be probed"),
    )
    try:
        with sessions.lock:
            sessions.sessions["active"].state = "error"
        report = scanner.scan().result(timeout=3)
        assert report["items"][0]["reason"] == "cached_session_unconfirmed"
        assert inventory.all()[0]["presenceState"] == "unknown"
    finally:
        scanner.close()
        sessions.close()
        repo.close()
