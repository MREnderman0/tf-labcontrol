from types import SimpleNamespace

import pytest

from lab_control.application.leases import ResourceLeases
from lab_control.application.sessions import SessionManager
from lab_control.discovery.dhcp import DhcpLease, DhcpLeaseDiscoveryProvider
from lab_control.discovery.inventory import Inventory
from lab_control.discovery.lan import LanDiscoverySettings, RememberedLanDiscoveryProvider
from lab_control.discovery.presence import is_current
from lab_control.discovery.providers import ConfiguredDiscoveryProvider
from lab_control.discovery.service import DiscoveryService
from lab_control.discovery.types import DiscoveryCandidate
from lab_control.domain.connections import ConnectionConfig, network_host
from lab_control.domain.instruments import DeviceError, DeviceIdentity
from lab_control.instruments.devices.kplusk_fxe.discovery import dhcp_discovery_config
from lab_control.storage.repository import Repository

MAC = "02:00:00:00:00:17"
SETTINGS = LanDiscoverySettings(enabled=True, networks=("192.0.2.0/24",))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    repository = Repository(tmp_path)
    leases = ResourceLeases()
    sessions = SessionManager(leases, repository, lambda _: pytest.fail("No control initialization"))
    inventory = Inventory(repository)
    config = ConnectionConfig(
        "192.0.2.10",
        "kplusk.fxe",
        "kplusk",
        timeout=7,
        expected_serial="TEST-517",
        options=("saved-option",),
        limits={"fixture_limit": 0.5},
    )
    identity = DeviceIdentity("K+K", "FXE2", "TEST-517", "E20-TEST", confidence="protocol")
    record = inventory.merge(DiscoveryCandidate(config, "configured"), "connected", identity, config.adapter)
    current = [DhcpLease(MAC, "192.0.2.10")]
    monkeypatch.setattr("lab_control.discovery.dhcp.local_dhcp_leases", lambda: tuple(current))
    provider = DhcpLeaseDiscoveryProvider(inventory, SETTINGS)
    calls = []

    def probe(candidate):
        calls.append(candidate)
        assert candidate.config.transport == "kplusk" and candidate.config.adapter == "kplusk.fxe"
        assert not candidate.safe_scpi
        assert not candidate.config.expected_serial and not candidate.config.options
        assert not candidate.config.limits and not candidate.config.aliases
        return identity, "kplusk.fxe"

    scanner = DiscoveryService((provider,), sessions, leases, inventory, probe)
    yield SimpleNamespace(**locals())
    scanner.close()
    sessions.close()
    repository.close()


def test_dhcp_follows_kplusk_after_reload_and_preserves_asset_config(setup):
    s = setup
    s.scanner.scan().result(5)
    assert s.record["dhcpBinding"] == {"mac": MAC, "identityKey": s.identity.key}
    s.inventory.records = Inventory(s.repository).records
    s.current[:] = [DhcpLease(MAC, "192.0.2.42")]
    s.scanner.scan().result(5)
    record = s.inventory.all()[0]
    assert len(s.inventory.all()) == 1 and record["id"] == s.record["id"]
    assert record["config"]["resource"] == "192.0.2.42" and is_current(record)
    assert record["config"]["expected_serial"] == "TEST-517"
    assert tuple(record["config"]["options"]) == ("saved-option",)
    assert record["config"]["limits"] == {"fixture_limit": 0.5}
    assert ConfiguredDiscoveryProvider((s.config,), s.inventory).enumerate() == ()
    assert RememberedLanDiscoveryProvider(s.inventory, SETTINGS).enumerate() == ()
    assert s.calls[-1].config.timeout == 7


def test_wrong_device_at_new_lease_cannot_replace_identity(setup):
    s = setup
    s.scanner.scan().result(5)
    s.current[:] = [DhcpLease(MAC, "192.0.2.42")]
    wrong = DeviceIdentity("K+K", "FXE2", "OTHER", "E20-TEST", confidence="protocol")
    s.scanner.probe = lambda _: (wrong, "kplusk.fxe")
    result = s.scanner.scan().result(5)
    assert result["items"][0]["errorCode"] == "identity_conflict"
    assert s.record["identity"]["serial"] == "TEST-517"
    assert s.record["config"]["resource"] == "192.0.2.10"
    assert not is_current(s.record)


@pytest.mark.parametrize("condition", ["missing", "foreign", "other_mac", "pinned", "active"])
def test_no_old_address_fallback_or_occupied_probe(setup, condition):
    s = setup
    s.scanner.scan().result(5)
    s.calls.clear()
    if condition == "missing":
        s.current.clear()
    elif condition == "foreign":
        s.current[:] = [DhcpLease(MAC, "198.51.100.42")]
    elif condition == "other_mac":
        s.current[:] = [DhcpLease("02:00:00:00:00:18", "192.0.2.10")]
    elif condition == "pinned":
        s.record["primaryResourceExplicit"] = True
    elif condition == "active":
        s.sessions.sessions["another-session"] = object()
    try:
        s.scanner.scan().result(5)
    finally:
        s.sessions.sessions.clear()
    assert not s.calls


@pytest.mark.parametrize(
    "resource,expected",
    [
        ("192.0.2.10", "192.0.2.42"),
        ("TCPIP0::192.0.2.10::10001::SOCKET", "192.0.2.42"),
        ("kplusk://192.0.2.10:1234", "kplusk://192.0.2.42:1234"),
    ],
)
def test_model_rebuilds_dhcp_resource_without_saved_identity(resource, expected):
    result = dhcp_discovery_config(
        ConnectionConfig(resource, "kplusk.fxe", "kplusk", expected_serial="OLD"), "192.0.2.42"
    )
    assert result.resource == expected and result.expected_serial == ""
    assert network_host(result.resource) == "192.0.2.42"


@pytest.mark.parametrize("resource", ["192.0.2.10", "kplusk://192.0.2.10:10001"])
def test_official_library_and_visa_share_same_host_lock(resource, tmp_path):
    leases = ResourceLeases(tmp_path)
    leases.acquire("counter", (resource,), identified=True)
    other = ResourceLeases(tmp_path)
    try:
        with pytest.raises(DeviceError) as exc:
            other.acquire("probe", ("TCPIP0::192.0.2.10::10001::SOCKET",), identified=True)
        assert exc.value.code == "resource_busy"
    finally:
        other.release("probe")
        leases.release("counter")
