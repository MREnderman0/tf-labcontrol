from types import SimpleNamespace

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.discovery.addresses import verified_connection
from lab_control.discovery.lan import (
    LanDiscoveryProvider,
    LanDiscoverySettings,
    ethernet_networks,
    load_lan_settings,
)
from lab_control.discovery.service import DiscoveryCandidate, Inventory
from lab_control.domain.instruments import DeviceError, DeviceIdentity, ValidationError
from lab_control.storage.repository import Repository


def candidate(host, **settings):
    return DiscoveryCandidate(
        ConnectionConfig(f"TCPIP0::{host}::inst0::INSTR", "rigol.dg5504pro", **settings),
        "lan",
        safe_scpi=True,
    )


def identity(serial="ASSET_A"):
    return DeviceIdentity("RIGOL", "DG5504 Pro", serial, "TEST")


@pytest.fixture
def inventory(tmp_path):
    repo = Repository(tmp_path)
    try:
        yield Inventory(repo)
    finally:
        repo.close()


def test_fresh_windows_install_uses_current_ethernet_not_saved_guids(tmp_path, monkeypatch):
    monkeypatch.setattr("lab_control.discovery.lan.sys.platform", "win32")
    settings = load_lan_settings(tmp_path)
    assert settings.enabled and settings.auto_ethernet and not settings.interfaces


def test_explicit_lan_scope_and_disabled_setting_remain_supported(tmp_path):
    with pytest.raises(ValidationError):
        LanDiscoverySettings(enabled=True)
    (tmp_path / "config").mkdir()
    (tmp_path / "config/lan-discovery.toml").write_text(
        'enabled=true\nnetworks=["192.0.2.0/24"]\ntimeout=2.5\n'
    )
    assert load_lan_settings(tmp_path).timeout == 2.5
    (tmp_path / "config/lan-discovery.toml").write_text("enabled=false\n")
    assert not load_lan_settings(tmp_path).enabled


@pytest.mark.parametrize("setting", ['auto_ethernet="yes"', 'auto_ethernet=true\ninterfaces=["old"]'])
def test_invalid_automatic_scope_is_rejected(tmp_path, setting):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/lan-discovery.toml").write_text(f"enabled=true\n{setting}\n")
    with pytest.raises(ValidationError):
        load_lan_settings(tmp_path)


def test_automatic_discovery_reloads_scope_after_unplug_and_changed_nic(monkeypatch):
    attached = iter([("192.0.2.0/24",), (), ("198.51.100.0/24",)])
    calls = []
    monkeypatch.setattr("lab_control.discovery.lan.ethernet_networks", lambda: next(attached))

    def discover(timeout):
        calls.append(timeout)
        return ("TCPIP0::192.0.2.10::inst0::INSTR", "TCPIP0::198.51.100.20::inst0::INSTR")

    provider = LanDiscoveryProvider(
        SimpleNamespace(discover_lan=discover),
        LanDiscoverySettings(enabled=True, auto_ethernet=True),
    )
    assert [item.descriptor["address"] for item in provider.enumerate()] == ["192.0.2.10"]
    assert provider.enumerate() == ()
    assert [item.descriptor["address"] for item in provider.enumerate()] == ["198.51.100.20"]
    assert calls == [3.0, 3.0]


def test_automatic_scope_respects_explicit_network_restriction(monkeypatch):
    monkeypatch.setattr("lab_control.discovery.lan.ethernet_networks", lambda: ("192.0.2.0/24",))
    provider = LanDiscoveryProvider(
        SimpleNamespace(discover_lan=lambda _: ("TCPIP0::192.0.2.10::inst0::INSTR",)),
        LanDiscoverySettings(enabled=True, networks=("198.51.100.0/24",), auto_ethernet=True),
    )
    assert provider.enumerate() == ()


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("[]", ()),
        ('{"ip":"192.0.2.20","prefix":24}', ("192.0.2.0/24",)),
        (
            '[{"ip":"192.0.2.20","prefix":24},{"ip":"192.0.2.30","prefix":24},'
            '{"ip":"169.254.20.30","prefix":16},{"ip":"127.0.0.1","prefix":8},'
            '{"ip":"0.0.0.0","prefix":0}]',
            ("169.254.0.0/16", "192.0.2.0/24"),
        ),
    ],
)
def test_current_ethernet_ipv4_response_normalization(monkeypatch, reply, expected):
    monkeypatch.setattr("lab_control.discovery.lan.sys.platform", "win32")
    monkeypatch.setattr(
        "lab_control.discovery.lan.subprocess.run", lambda *args, **kwargs: SimpleNamespace(stdout=reply)
    )
    assert ethernet_networks() == expected


def test_filter_before_identity_no_socket_or_foreign_network(monkeypatch):
    calls = []

    def discover(timeout):
        calls.append(timeout)
        return (
            "TCPIP0::192.0.2.10::inst0::INSTR",
            "TCPIP0::198.51.100.1::inst0::INSTR",
            "TCPIP0::192.0.2.10::5025::SOCKET",
        )

    monkeypatch.setattr("lab_control.discovery.lan.interface_networks", lambda _: ("192.0.2.0/24",))
    provider = LanDiscoveryProvider(
        SimpleNamespace(discover_lan=discover), LanDiscoverySettings(True, ("LAB_GUID",), (), 2)
    )
    found = provider.enumerate()
    assert (
        len(found) == 1
        and found[0].safe_scpi
        and found[0].config.resource == "TCPIP0::192.0.2.10::inst0::INSTR"
    )
    assert calls == [2]


def test_ip_move_preserves_identity_limits_slots_and_name(inventory):
    first = inventory.merge(
        candidate("192.0.2.10", expected_serial="ASSET_A", limits={"amplitude": 0.5}, timeout=4),
        "old",
        identity(),
        "rigol.dg5504pro",
        reason="identified",
    )
    inventory.edit(first["id"], display_name="信号源 A", slot="signal_generator:1")
    inventory.merge(candidate("192.0.2.20"), "new", reason="pending_probe")
    updated = inventory.merge(
        candidate("192.0.2.20"), "new", identity(), "rigol.dg5504pro", reason="identified"
    )
    inventory.stale("new", {"lan"})
    assert updated["id"] == first["id"] and len(inventory.all()) == 1
    assert updated["displayName"] == "信号源 A" and updated["slots"]["general"] == "signal_generator:1"
    config = verified_connection(updated)
    assert config.resource.endswith("192.0.2.20::inst0::INSTR")
    assert config.timeout == 4 and config.expected_serial == "ASSET_A" and config.limits == {"amplitude": 0.5}
    assert not config.aliases


def test_reused_ip_does_not_transfer_asset_settings(inventory):
    old = inventory.merge(
        candidate("192.0.2.10", expected_serial="ASSET_A", limits={"amplitude": 0.2}),
        "old",
        identity(),
        "rigol.dg5504pro",
        reason="identified",
    )
    replacement = inventory.merge(
        candidate("192.0.2.10"), "new", identity("ASSET_B"), "rigol.dg5504pro", reason="identified"
    )
    assert old["id"] != replacement["id"] and len(inventory.all()) == 2
    assert replacement["config"]["limits"] == {} and replacement["config"]["expected_serial"] == "ASSET_B"
    with pytest.raises(DeviceError):
        verified_connection(old)
    assert inventory.by_resource(candidate("192.0.2.10").config.resource)["id"] == replacement["id"]


def test_fixed_resource_is_not_silently_changed(inventory):
    old = inventory.merge(candidate("192.0.2.10"), "old", identity(), "rigol.dg5504pro", reason="identified")
    inventory.select_resource(old["id"], old["config"]["resource"])
    updated = inventory.merge(
        candidate("192.0.2.20"), "new", identity(), "rigol.dg5504pro", reason="identified"
    )
    assert updated["config"]["resource"] == old["config"]["resource"]


def test_selected_lan_follows_identity_after_restart_and_keeps_usb_and_settings(inventory):
    usb = DiscoveryCandidate(
        ConnectionConfig(
            "USB0::0x1234::0x0001::ASSET_A::INSTR",
            "rigol.dg5504pro",
            timeout=8,
            limits={"amplitude": 0.5},
            options=("OPTION",),
        ),
        "visa",
        safe_scpi=True,
    )
    original = inventory.merge(usb, "initial", identity(), usb.config.adapter, reason="identified")
    inventory.edit(original["id"], display_name="实验信号源", slot="signal_generator:1")
    first_lan = candidate("192.0.2.10")
    inventory.merge(first_lan, "initial", identity(), usb.config.adapter, reason="identified")
    inventory.select_resource(original["id"], first_lan.config.resource, follow_address=True)
    restarted = Inventory(inventory.repository)
    assert restarted.all()[0]["config"]["resource"] == first_lan.config.resource
    moved = restarted.merge(
        candidate("192.0.2.20"), "later", identity(), usb.config.adapter, reason="identified"
    )
    restarted.stale("later", {"lan", "visa"})
    selected = verified_connection(moved)
    assert selected.resource == candidate("192.0.2.20").config.resource
    assert selected.expected_serial == "ASSET_A" and selected.timeout == 8
    assert selected.limits == {"amplitude": 0.5} and tuple(selected.options) == ("OPTION",)
    assert moved["id"] == original["id"] and moved["displayName"] == "实验信号源"
    assert moved["slots"]["general"] == "signal_generator:1"
    assert usb.config.resource in moved["resources"] and not moved["primaryResourceExplicit"]
    # Rediscovering the USB interface must not undo the user's LAN preference.
    assert restarted.merge(usb, "later", identity(), usb.config.adapter)["config"] == moved["config"]


def test_lan_follow_rejects_unverified_candidate_without_changing_primary(inventory):
    original = inventory.merge(candidate("192.0.2.10"), "old", identity(), "rigol.dg5504pro")
    unverified = candidate("192.0.2.20")
    from copy import deepcopy

    original["resources"].append(unverified.config.resource)
    original["candidates"][unverified.config.resource] = {"config": {"transport": "visa"}}
    before = deepcopy(original)
    with pytest.raises(DeviceError) as exc:
        inventory.select_resource(original["id"], unverified.config.resource, follow_address=True)
    assert exc.value.code == "identity_required" and original == before


def test_lan_follow_rejects_conflicting_serial(inventory):
    original = inventory.merge(
        candidate("192.0.2.10", expected_serial="DIFFERENT"), "old", identity(), "rigol.dg5504pro"
    )
    with pytest.raises(DeviceError) as exc:
        inventory.select_resource(original["id"], original["config"]["resource"], follow_address=True)
    assert exc.value.code == "identity_conflict"
    assert original["config"]["expected_serial"] == "DIFFERENT"


@pytest.mark.parametrize("session_label", ["connected", "manual"])
def test_historical_connections_at_changed_ip_do_not_create_duplicate_identity(inventory, session_label):
    first = inventory.merge(candidate("192.0.2.10"), session_label, identity(), "rigol.dg5504pro")
    updated = inventory.merge(
        candidate("192.0.2.20"), "new-scan", identity(), "rigol.dg5504pro", reason="identified"
    )
    connected = inventory.merge(candidate("192.0.2.20"), session_label, identity(), "rigol.dg5504pro")
    assert first["id"] == updated["id"] == connected["id"]
    assert not connected.get("identityConflict")
    assert verified_connection(connected).resource == candidate("192.0.2.20").config.resource


@pytest.mark.parametrize("old_scan", ["connected", "manual", "real-scan"])
def test_fresh_identity_repairs_only_legacy_reused_session_label_conflict(inventory, old_scan):
    record = inventory.merge(candidate("192.0.2.10"), "old", identity(), "rigol.dg5504pro")
    record["identityConflict"] = {"reason": "same_identity_at_multiple_network_hosts", "scanRunId": old_scan}
    record["discoveryState"] = "conflict"
    inventory.repository.save(record)
    restarted = Inventory(inventory.repository)
    # Reading saved metadata alone must not remove the conflict or authorize connection.
    with pytest.raises(DeviceError):
        verified_connection(restarted.all()[0])
    updated = restarted.merge(
        candidate("192.0.2.20"), "fresh", identity(), "rigol.dg5504pro", reason="identified"
    )
    if old_scan == "real-scan":
        with pytest.raises(DeviceError):
            verified_connection(updated)
        assert updated["identityConflict"]["scanRunId"] == old_scan
    else:
        assert not updated.get("identityConflict")
        assert verified_connection(updated).resource == candidate("192.0.2.20").config.resource


def test_duplicate_lan_identity_and_missing_serial_are_not_silently_trusted(inventory):
    a = inventory.merge(candidate("192.0.2.10"), "scan", identity(), "rigol.dg5504pro", reason="identified")
    b = inventory.merge(candidate("192.0.2.20"), "scan", identity(), "rigol.dg5504pro", reason="identified")
    assert a["id"] == b["id"] and b["discoveryState"] == "conflict"
    c = inventory.merge(candidate("192.0.2.30"), "scan", identity(""), "rigol.dg5504pro", reason="identified")
    d = inventory.merge(candidate("192.0.2.40"), "scan", identity(""), "rigol.dg5504pro", reason="identified")
    assert c["id"] != d["id"]
    for record in (a, b, c, d):
        with pytest.raises(DeviceError):
            verified_connection(record)


def test_usb_and_two_lan_protocols_merge_without_replacing_usb_primary(inventory):
    usb = DiscoveryCandidate(
        ConnectionConfig("USB0::0x1234::0x0001::TEST::INSTR", "rigol.dg5504pro"), "visa", safe_scpi=True
    )
    first = inventory.merge(usb, "scan", identity(), "rigol.dg5504pro", reason="identified")
    inventory.merge(candidate("192.0.2.5"), "scan", identity(), "rigol.dg5504pro", reason="identified")
    hislip = DiscoveryCandidate(ConnectionConfig("TCPIP0::192.0.2.5::hislip0::INSTR"), "lan", safe_scpi=True)
    result = inventory.merge(hislip, "scan", identity(), "rigol.dg5504pro", reason="identified")
    assert result["id"] == first["id"] and len(inventory.all()) == 1
    assert result["config"]["resource"] == usb.config.resource
    assert len(verified_connection(result).aliases) == 2


def test_twenty_assets_change_addresses_without_changing_asset_ids(inventory):
    ids = []
    for index in range(20):
        ids.append(
            inventory.merge(
                candidate(f"192.0.2.{index + 1}"),
                "before",
                identity(f"TEST{index}"),
                "rigol.dg5504pro",
                reason="identified",
            )["id"]
        )
    for index in range(20):
        current = inventory.merge(
            candidate(f"192.0.2.{index + 101}"),
            "after",
            identity(f"TEST{index}"),
            "rigol.dg5504pro",
            reason="identified",
        )
        assert current["id"] == ids[index]
    inventory.stale("after", {"lan"})
    assert len(inventory.all()) == 20
    assert all(not verified_connection(item).aliases for item in inventory.all())
