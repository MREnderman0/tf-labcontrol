from lab_control.application.config import ConnectionConfig
from lab_control.application.leases import ResourceLeases
from lab_control.application.sessions import SessionManager
from lab_control.application.static_addresses import StaticAddressStore
from lab_control.demo.instrument import SimulatedInstrument
from lab_control.discovery.lan import StaticAddressDiscoveryProvider
from lab_control.discovery.service import DiscoveryCandidate, DiscoveryService, Inventory
from lab_control.domain.instruments import DeviceIdentity
from lab_control.storage.repository import Repository


def test_targeted_static_scan_does_not_mark_other_inventory_absent(tmp_path):
    repository = Repository(tmp_path)
    leases = ResourceLeases(tmp_path)
    sessions = SessionManager(leases, repository, SimulatedInstrument)
    inventory = Inventory(repository)
    scanner = None
    try:
        saved = StaticAddressStore(repository)
        entry = saved.add("192.168.0.120")
        candidate = DiscoveryCandidate(
            ConnectionConfig("TCPIP0::192.168.0.121::10001::SOCKET", "ceyear.6362d"),
            "static_addresses",
        )
        inventory.merge(candidate, "old", DeviceIdentity("Ceyear", "6362D", "OTHER"), "ceyear.6362d")
        scanner = DiscoveryService(
            (StaticAddressDiscoveryProvider(saved),),
            sessions,
            leases,
            inventory,
            lambda _candidate: (DeviceIdentity("Ceyear", "6362D", "TARGET"), "ceyear.6362d"),
        )
        result = scanner.scan(
            (StaticAddressDiscoveryProvider(saved).for_ids((entry.id,)),),
            preserve_presence=True,
            scope="address_list",
        ).result(timeout=5)
        assert result["scope"] == "address_list"
        assert result["providers"]["static_addresses"]["count"] == 1
        other = inventory.by_resource(candidate.config.resource)
        assert other is not None
        assert other["presenceState"] != "not_seen"
    finally:
        if scanner is not None:
            scanner.close()
        sessions.close()
        repository.close()


def test_static_scan_persists_model_normalized_tcp_source_config(tmp_path):
    repository = Repository(tmp_path)
    leases = ResourceLeases(tmp_path)
    sessions = SessionManager(leases, repository, SimulatedInstrument)
    inventory = Inventory(repository)
    scanner = None
    try:
        store = StaticAddressStore(repository)
        entry = store.add("192.168.5.241")
        old = ConnectionConfig(entry.resource, "ceyear.6362d")
        existing = inventory.merge(
            DiscoveryCandidate(old, "configured"),
            "old",
            DeviceIdentity("Ceyear Technologies", "CY6362D", "SERIAL-A", "2.2.1"),
            "ceyear.6362d",
        )
        scanner = DiscoveryService(
            (StaticAddressDiscoveryProvider(store),),
            sessions,
            leases,
            inventory,
            lambda _candidate: (
                DeviceIdentity("Ceyear Technologies", "CY6362D", "SERIAL-A", "2.2.1"),
                "ceyear.6362d",
            ),
            normalize_discovery_config=lambda config, sources: config.__class__(
                config.resource,
                config.adapter,
                "tcp",
                timeout=config.timeout,
                source_address=sources[0],
            ),
            source_addresses=lambda: ("192.168.0.1",),
        )
        result = scanner.scan().result(timeout=5)
        assert result["items"][0]["reason"] == "identified"
        record = next(item for item in inventory.all() if item["id"] == existing["id"])
        assert record["presenceState"] == "present"
        assert record["config"]["transport"] == "tcp"
        assert record["config"]["source_address"] == "192.168.0.1"
    finally:
        if scanner is not None:
            scanner.close()
        sessions.close()
        repository.close()
