"""Shared discovery admission, with no physical transports or vendor drivers."""

import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.leases import ResourceLeases
from lab_control.application.sessions import SessionManager
from lab_control.demo.instrument import SimulatedInstrument
from lab_control.discovery.probe_lifetime import probe_handle
from lab_control.discovery.service import (
    DiscoveryCandidate,
    DiscoveryService,
    Inventory,
)
from lab_control.domain.instruments import DeviceError, DeviceIdentity
from lab_control.instruments.registry import DEVICE_DEFINITIONS
from lab_control.storage.repository import Repository


@pytest.fixture
def lab(tmp_path):
    repo = Repository(tmp_path)
    leases = ResourceLeases(tmp_path / "locks")
    sessions = SessionManager(leases, repo, SimulatedInstrument)
    inventory = Inventory(repo)
    scanners = []

    def scanner(candidates, probe):
        provider = SimpleNamespace(name="fixture", enumerate=lambda: tuple(candidates))
        value = DiscoveryService((provider,), sessions, leases, inventory, probe)
        scanners.append(value)
        return value

    yield SimpleNamespace(repo=repo, leases=leases, sessions=sessions, inventory=inventory, scanner=scanner)
    for value in scanners:
        value.close()
    sessions.close()
    repo.close()


def configured(resource, adapter="rigol.dg5504pro", **kwargs):
    return DiscoveryCandidate(ConnectionConfig(resource, adapter, **kwargs), "configured")


def connect(lab, candidate, name="active"):
    return lab.sessions.connect(name, candidate.config, configure_acquisition=False).result(5)


def identified(candidate):
    definition = DEVICE_DEFINITIONS[candidate.config.adapter]
    model = definition.profile or definition.model
    return DeviceIdentity(
        model.manufacturer[0], model.models[0], candidate.config.resource
    ), definition.adapter_id


MODEL_ROUTES = [
    (key, transport)
    for key, definition in DEVICE_DEFINITIONS.items()
    for transport in sorted(
        set(definition.discovery_connection_only_transports)
        | {definition.supported_transports[0] if definition.supported_transports else "visa"}
    )
]


@pytest.mark.parametrize(("adapter", "transport"), MODEL_ROUTES)
def test_registered_routes_respect_independent_sessions_and_model_policy(lab, adapter, transport):
    connect(lab, configured("TCPIP0::192.0.2.10::inst0::INSTR", "rigol.m300"))
    target = configured("TCPIP0::192.0.2.20::5025::SOCKET", adapter, transport=transport)
    calls = []
    scan = lab.scanner([target], lambda c: (calls.append(c), identified(c))[1])
    result = scan.scan().result(5)["items"][0]
    if transport in DEVICE_DEFINITIONS[adapter].discovery_connection_only_transports:
        assert result["reason"] == "sdk_identity_requires_explicit_connection"
        assert calls == []
    else:
        assert result["reason"] == "identified"
        assert calls == [target]


def test_two_scopes_are_discovered_with_m300_and_dho_sessions_untouched(lab, monkeypatch):
    active = [
        configured("TCPIP0::192.0.2.10::inst0::INSTR", "rigol.m300"),
        configured("TCPIP0::192.0.2.11::inst0::INSTR", "rigol.dho924"),
    ]
    for n, candidate in enumerate(active):
        connect(lab, candidate, str(n))
        adapter = lab.sessions.sessions[str(n)].adapter
        monkeypatch.setattr(adapter, "identify", lambda: pytest.fail("Active identity was queried"))
    before = lab.sessions.snapshots()
    targets = [configured(f"TCPIP0::192.0.2.{n}::10001::SOCKET", "ceyear.6362d") for n in (21, 22)]
    scan = lab.scanner(active + targets, identified)
    result = scan.scan().result(5)
    assert [r["reason"] for r in result["items"]] == [
        "cached_active_session",
        "cached_active_session",
        "identified",
        "identified",
    ]
    assert lab.sessions.snapshots() == before


@pytest.mark.parametrize("same_resource", [True, False])
def test_failed_connection_with_confirmed_cleanup_does_not_block(lab, same_resource):
    target = configured("mock://failed", transport="mock")

    class Failing(SimulatedInstrument):
        def identify(self):
            raise DeviceError("fixture timeout", "timeout")

    lab.sessions.factory = Failing
    with pytest.raises(DeviceError):
        connect(lab, target)
    session = lab.sessions.sessions["active"]
    assert session.resource_state == "released" and session.adapter is None
    assert not lab.leases.owns_resources("active")
    if not same_resource:
        target = configured("mock://unrelated", transport="mock")
    assert lab.scanner([target], identified).scan().result(5)["items"][0]["reason"] == "identified"
    assert session.last_error == "fixture timeout"


def test_failed_close_retains_reference_and_claims_until_explicit_retry(lab):
    target = configured("mock://failed", transport="mock")

    class Failing(SimulatedInstrument):
        fail_close = True

        def identify(self):
            raise DeviceError("fixture identity failure", "timeout")

        def close(self):
            if self.fail_close:
                raise OSError("fixture close failed")
            super().close()

    lab.sessions.factory = Failing
    with pytest.raises(DeviceError):
        connect(lab, target)
    session = lab.sessions.sessions["active"]
    assert session.resource_state == "release_unconfirmed"
    assert session.adapter is not None and lab.leases.owns_resources("active")
    scan = lab.scanner([target], lambda _: pytest.fail("Unreleased handle was reopened"))
    assert scan.scan().result(5)["items"][0]["reason"] == "cached_session_unconfirmed"
    session.adapter.fail_close = False
    lab.sessions.disconnect("active")
    assert session.resource_state == "released" and session.adapter is None
    assert not lab.leases.owns_resources("active")
    assert lab.scanner([target], identified).scan().result(5)["items"][0]["reason"] == "identified"


@pytest.mark.parametrize(
    ("resource", "alias", "extra", "expected"),
    [
        ("COM4", "ASRL4::INSTR", {}, "cached_active_session"),
        ("tcp://192.0.2.10:5025", "TCPIP0::192.0.2.10::5025::SOCKET", {}, "cached_active_session"),
        (
            "TCPIP0::192.0.2.10::inst0::INSTR",
            "vicp://192.0.2.10:1861",
            {},
            "deferred_possible_alias_of_active_device",
        ),
        ("COM4", "COM8", {"usb_instance_id": "BRIDGE-A"}, "deferred_possible_alias_of_active_device"),
        (
            "USB::A",
            "TCPIP0::192.0.2.20::INSTR",
            {"expected_serial": "ASSET-A"},
            "deferred_possible_alias_of_active_device",
        ),
    ],
)
def test_resource_host_bridge_and_asset_claims_protect_active_handles(lab, resource, alias, extra, expected):
    connect(lab, configured(resource, **extra))
    candidate = configured(alias, **extra)
    result = lab.scanner([candidate], lambda _: pytest.fail("Alias was opened")).scan().result(5)
    assert result["items"][0]["reason"] == expected
    assert result["items"][0]["blockingOwners"] == ["active"]


def test_unknown_network_candidate_still_defers(lab):
    connect(lab, configured("mock://known", transport="mock"))
    candidate = DiscoveryCandidate(ConnectionConfig("TCPIP0::192.0.2.25::INSTR"), "lan", safe_scpi=True)
    result = lab.scanner([candidate], lambda _: pytest.fail("Unknown alias opened")).scan().result(5)
    assert result["items"][0]["reason"] == "deferred_possible_alias_of_active_device"


def test_shared_probe_gate_blocks_cross_interface_connection_and_releases_on_cancel(lab):
    entered, release = threading.Event(), threading.Event()
    target = configured("USB::TARGET")

    def probe(c):
        entered.set()
        assert release.wait(5)
        return identified(c)

    scanner = lab.scanner([target], probe)
    future = scanner.scan()
    try:
        assert entered.wait(3)
        assert lab.leases.probes_active()
        with pytest.raises(DeviceError, match="probe"):
            connect(lab, configured("TCPIP0::192.0.2.30::INSTR"))
        scanner.cancel()
    finally:
        release.set()
        future.result(5)
    assert not lab.leases.probes_active() and not lab.leases.conflicts((target.config.resource,))
    connect(lab, configured("mock://after", transport="mock"))


def test_serial_profile_inference_never_copies_another_assets_configuration(lab):
    original = configured(
        "COM4",
        "joinwit.jw8105",
        transport="serial",
        baud_rate=115200,
        expected_serial="LABEL-A",
        limits={"power": 1},
        options=("asset-option",),
        usb_instance_id="BRIDGE-A",
        aliases=("ASRL4::INSTR",),
    )
    descriptor = {"vid": 0x403, "pid": 0x6001, "usb_serial": "BRIDGE-A"}
    lab.inventory.merge(
        replace(original, source="serial", descriptor=descriptor), "seed", adapter=original.config.adapter
    )
    profile = lab.inventory.serial_profile({**descriptor, "usb_serial": "BRIDGE-B"})
    assert profile.adapter == "joinwit.jw8105" and profile.baud_rate == 115200
    assert not (
        profile.expected_serial
        or profile.limits
        or profile.options
        or profile.aliases
        or profile.usb_instance_id
    )


def test_verified_primary_can_probe_but_a_second_interface_of_active_asset_cannot(lab):
    current = configured("USB::saved-asset", expected_serial="VERIFIED-A")
    identity = connect(lab, current)
    address = "TCPIP0::192.0.2.45::5025::SOCKET"
    target = DiscoveryCandidate(ConnectionConfig(address), "remembered_lan", safe_scpi=True)
    lab.inventory.merge(target, "previous", identity, current.config.adapter, reason="identified")
    scan = lab.scanner([target], lambda _: (identity, current.config.adapter))
    item = scan.scan().result(5)["items"][0]
    assert item["reason"] == "deferred_possible_alias_of_active_device"
    assert lab.inventory.by_resource(address)["presenceState"] == "unknown"
    lab.sessions.disconnect("active")
    connect(lab, configured("mock://unrelated", transport="mock"), "other")
    assert scan.scan().result(5)["items"][0]["reason"] == "identified"


def test_dhcp_expectation_is_checked_even_for_cached_active_resource(lab):
    current = configured("TCPIP0::192.0.2.30::inst0::INSTR")
    connect(lab, current)
    wrong = DeviceIdentity("RIGOL", "DG5504 Pro", "OLD-ASSET")
    old = DiscoveryCandidate(ConnectionConfig("TCPIP0::192.0.2.25::inst0::INSTR"), "lan", safe_scpi=True)
    lab.inventory.merge(old, "previous", wrong, current.config.adapter, reason="identified")
    target = DiscoveryCandidate(current.config, "dhcp_leases", {"expectedIdentityKey": wrong.key}, True)
    item = (
        lab.scanner([target], lambda _: pytest.fail("Active endpoint queried")).scan().result(5)["items"][0]
    )
    assert item["reason"] == "probe_failed" and item["errorCode"] == "identity_conflict"
    assert lab.sessions.sessions["active"].state == "connected"


def test_three_model_only_meters_and_changed_port_keep_separate_assets(lab):
    class ModelOnly(SimulatedInstrument):
        def identify(self):
            return DeviceIdentity("Joinwit", "JW8105", confidence="model_only")

    lab.sessions.factory = ModelOnly
    candidates = [
        configured(f"COM{n}", "joinwit.jw8105", transport="serial", usb_instance_id=f"BRIDGE-{n}")
        for n in (3, 4, 5)
    ]

    def probe(c):
        return DeviceIdentity("Joinwit", "JW8105", confidence="model_only"), c.config.adapter

    for n, c in enumerate(candidates):
        identity = connect(lab, c, str(n))
        record = lab.inventory.merge(c, "previous", identity, c.config.adapter, reason="identified")
        record["displayName"] = f"meter-{n}"
    before = {r["config"]["usb_instance_id"]: r["id"] for r in lab.inventory.all()}
    changed = replace(candidates[2], config=replace(candidates[2].config, resource="COM15"), source="serial")
    # A re-enumerated bridge is still owned until its original session closes.
    assert (
        lab.scanner([changed], probe).scan().result(5)["items"][0]["reason"]
        == "deferred_possible_alias_of_active_device"
    )
    lab.sessions.disconnect("2")
    assert lab.scanner([changed], probe).scan().result(5)["items"][0]["reason"] == "identified"
    after = {r["config"]["usb_instance_id"]: r["id"] for r in lab.inventory.all()}
    assert before == after and len(after) == 3
    assert all(not r["identity"].get("serial") for r in lab.inventory.all())


def test_connection_reservation_protects_alias_before_identity_finishes(lab):
    entered, release = threading.Event(), threading.Event()

    class Slow(SimulatedInstrument):
        def identify(self):
            entered.set()
            assert release.wait(5)
            return super().identify()

    lab.sessions.factory = Slow
    target = configured("COM4", usb_instance_id="BRIDGE-A")
    future = lab.sessions.connect("active", target.config, configure_acquisition=False)
    try:
        assert entered.wait(3)
        alias = configured("COM9", usb_instance_id="BRIDGE-A")
        scan = lab.scanner([target, alias], lambda _: pytest.fail("Connecting device queried"))
        assert [i["reason"] for i in scan.scan().result(3)["items"]] == [
            "cached_session_unconfirmed",
            "deferred_possible_alias_of_active_device",
        ]
    finally:
        release.set()
        future.result(5)


def test_uncertain_probe_close_preserves_gate_and_handle(lab):
    class Handle:
        fails = True

        def close(self):
            if self.fails:
                raise OSError("fixture probe close failed")

    handle = Handle()

    def probe(c):
        with probe_handle(handle):
            return identified(c)

    target = configured("USB::uncertain")
    scan = lab.scanner([target], probe)
    try:
        item = scan.scan().result(5)["items"][0]
        assert item["reason"] == "probe_failed" and item["errorCode"] == "cleanup_failed"
        assert scan.unreleased_probes and lab.leases.probes_active()
        assert scan.scan().result(5)["items"][0]["reason"] == "deferred_possible_alias_of_active_device"
        with pytest.raises(DeviceError):
            connect(lab, configured("mock://other", transport="mock"))
    finally:
        handle.fails = False
        scan.close()
    assert not lab.leases.probes_active()


def test_failed_os_lease_release_is_retryable_without_losing_owner(lab, monkeypatch):
    target = configured("mock://lease-close", transport="mock")
    connect(lab, target)
    files = lab.leases._files["active"]
    gate = files["gate"]
    original = gate.close

    def fail():
        raise OSError("fixture OS lock close failed")

    monkeypatch.setattr(gate, "close", fail)
    with pytest.raises(OSError):
        lab.sessions.disconnect("active")
    assert lab.leases.owner(target.config.resource) == "active"
    assert lab.sessions.sessions["active"].resource_state == "release_unconfirmed"
    monkeypatch.setattr(gate, "close", original)
    lab.sessions.disconnect("active")
    assert not lab.leases.owns_resources("active")


def test_failed_factory_returns_uncertain_partial_transport_to_session(lab, monkeypatch):
    from lab_control.instruments import registry

    class PartialTransport:
        fails = True
        closes = 0

        def open(self):
            raise OSError("fixture open failed after creating handle")

        def close(self):
            self.closes += 1
            if self.fails:
                raise OSError("fixture partial handle close failed")

    transport = PartialTransport()
    monkeypatch.setattr(registry, "transport_for", lambda *_: transport)
    lab.sessions.factory = lambda config: registry.create_adapter(config, None)
    candidate = configured("USB::partial")
    with pytest.raises(DeviceError, match="release unconfirmed"):
        connect(lab, candidate)
    session = lab.sessions.sessions["active"]
    assert session.cleanup_handle is transport and transport.closes == 1
    assert session.resource_state == "release_unconfirmed"
    assert lab.leases.owner(candidate.config.resource) == "active"
    transport.fails = False
    lab.sessions.disconnect("active")
    assert session.cleanup_handle is None and transport.closes == 2
    assert not lab.leases.owns_resources("active")
