from copy import deepcopy

import pytest

from lab_control.application.network_setup import plan_dhcp, validate_plan
from lab_control.cli.main import main
from lab_control.domain.instruments import ValidationError


def snapshot(host="computer-a", guid="nic-a", ip="10.42.0.2", prefix=24):
    return {
        "host_fingerprint": host,
        "adapters": [
            {
                "id": guid,
                "name": "Lab Ethernet",
                "up": True,
                "ethernet": True,
                "addresses": [{"ip": ip, "prefix": prefix, "origin": "Manual"}],
                "gateways": [],
            }
        ],
        "dhcp_listeners": [],
    }


def test_different_computers_generate_their_own_subnet_and_interface():
    a = plan_dhcp(snapshot(), "nic-a")
    b = plan_dhcp(snapshot("computer-b", "usb-nic", "172.23.9.10"), "usb-nic")
    assert a["network"] == "10.42.0.0/24"
    assert b["network"] == "172.23.9.0/24"
    assert b["pool_start"] == "172.23.9.11"
    assert b["client_policy"] == "all_dhcp_clients_on_selected_lab_interface"
    assert b["pool_size"] == 100 and "reservations" not in b
    assert a["state"] == "prepared_only" and a["remaining_checks"]


@pytest.mark.parametrize("change", ["host", "nic", "subnet", "prefix", "disconnected"])
def test_moved_or_stale_plan_is_rejected(change):
    before = snapshot()
    plan = plan_dhcp(before, "nic-a")
    validate_plan(plan, before)
    after = deepcopy(before)
    if change == "host":
        after["host_fingerprint"] = "computer-b"
    elif change == "nic":
        after["adapters"][0]["id"] = "replacement-usb-nic"
    elif change == "subnet":
        after["adapters"][0]["addresses"][0]["ip"] = "10.44.0.2"
    elif change == "prefix":
        after["adapters"][0]["addresses"][0]["prefix"] = 23
    else:
        after["adapters"][0]["up"] = False
    with pytest.raises(ValidationError):
        validate_plan(plan, after)


@pytest.mark.parametrize(
    "settings",
    [
        {"pool_size": 0},
        {"pool_size": True},
        {"pool_start": "10.42.0.0"},
        {"pool_start": "10.42.0.200"},
        {"pool_start": "10.43.0.10"},
        {"pool_start": "10.42.0.1"},
        {"lease_minutes": 0},
        {"pool_start": "10.42.0.100", "exclusions": ("10.42.0.120",)},
    ],
)
def test_pool_does_not_overlap_server_reserved_addresses_or_subnet_boundaries(settings):
    with pytest.raises(ValidationError):
        plan_dhcp(snapshot(), "nic-a", **settings)


def test_automatic_pool_avoids_explicit_static_reservations_and_all_local_addresses():
    state = snapshot()
    state["adapters"][0]["addresses"].append({"ip": "169.254.30.20", "prefix": 16, "origin": "Manual"})
    plan = plan_dhcp(state, "Lab Ethernet", exclusions=("10.42.0.80",))
    assert plan["pool_start"] == "10.42.0.81"
    assert plan["pool_end"] == "10.42.0.180"


def test_existing_dhcp_client_interface_and_wifi_are_not_silently_used_as_server():
    state = snapshot()
    state["adapters"][0]["addresses"][0]["origin"] = "Dhcp"
    with pytest.raises(ValidationError, match="already uses DHCP"):
        plan_dhcp(state, "nic-a")
    state = snapshot()
    state["adapters"][0]["ethernet"] = False
    with pytest.raises(ValidationError, match="physical Ethernet"):
        plan_dhcp(state, "nic-a")


def test_multiple_addresses_require_explicit_current_server_selection():
    state = snapshot()
    state["adapters"][0]["addresses"].append({"ip": "172.16.4.2", "prefix": 24, "origin": "Manual"})
    with pytest.raises(ValidationError):
        plan_dhcp(state, "nic-a")
    assert plan_dhcp(state, "nic-a", server_address="172.16.4.2")["network"] == "172.16.4.0/24"


def test_changed_activation_values_rejected_even_with_same_host():
    plan = plan_dhcp(snapshot(), "nic-a")
    plan["dhcp_settings"]["PoolSize"] = 0
    with pytest.raises(ValidationError):
        validate_plan(plan, snapshot())


def test_dedicated_lab_plan_rejects_gateway_other_interface_and_vpn_routes():
    state = snapshot()
    state["adapters"][0]["gateways"] = ["10.42.0.1"]
    with pytest.raises(ValidationError, match="dedicated"):
        plan_dhcp(state, "nic-a")
    state = snapshot()
    other = deepcopy(state["adapters"][0])
    other["id"] = "other-nic"
    state["adapters"].append(other)
    with pytest.raises(ValidationError, match="overlaps another interface"):
        plan_dhcp(state, "nic-a")
    state = snapshot()
    state["routes"] = [{"InterfaceIndex": 42, "DestinationPrefix": "10.0.0.0/8"}]
    with pytest.raises(ValidationError, match="VPN route"):
        plan_dhcp(state, "nic-a")


def test_restore_snapshot_stays_local_and_stale_dns_is_rejected():
    state = snapshot()
    state["dns"] = [{"InterfaceIndex": 3, "ServerAddresses": []}]
    plan = plan_dhcp(state, "nic-a")
    assert not plan["executor_available"]
    assert plan["restore_snapshot"]["adapter"]["id"] == "nic-a"
    state["dns"] = [{"InterfaceIndex": 3, "ServerAddresses": ["192.0.2.1"]}]
    with pytest.raises(ValidationError, match="restore snapshot"):
        validate_plan(plan, state)


def test_cli_preparation_and_validation_never_open_hardware_or_overwrite_plan(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("lab_control.application.network_setup.inspect_network", snapshot)

    def forbidden(*args, **kwargs):
        raise AssertionError("Network planning must not initialize instruments")

    monkeypatch.setattr("lab_control.cli.main.ApplicationRuntime", forbidden)
    path = tmp_path / "network-plan.json"
    assert main(["network", "inspect"]) == 0
    assert main(["network", "plan", "--interface", "nic-a", "--plan-file", str(path)]) == 0
    before = path.read_bytes()
    assert main(["network", "plan", "--interface", "nic-a", "--plan-file", str(path)]) == 2
    assert path.read_bytes() == before
    assert main(["network", "validate", "--plan-file", str(path)]) == 0
    monkeypatch.setattr(
        "lab_control.application.network_setup.inspect_network", lambda: snapshot("computer-b")
    )
    assert main(["network", "validate", "--plan-file", str(path)]) == 2
    assert "another computer" in capsys.readouterr().out
