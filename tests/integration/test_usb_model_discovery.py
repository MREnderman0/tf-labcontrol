import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.leases import ResourceLeases
from lab_control.application.sessions import SessionManager
from lab_control.discovery.service import ConfiguredDiscoveryProvider, DiscoveryService, Inventory
from lab_control.instruments.devices.microchip_53100a import discovery
from lab_control.storage.repository import Repository


def test_model_pnp_failure_survives_configured_candidate_merge_without_io(tmp_path, monkeypatch):
    usb = r"USB\VID_16D0&PID_05B8\SYNTHETIC-USB"
    monkeypatch.setattr(
        discovery, "enumerate_devices", lambda: ({"PNPDeviceID": usb, "ConfigManagerErrorCode": 28},)
    )
    config = ConnectionConfig(
        "USBPNP::" + usb, "microchip.53100a", "tserve", usb_instance_id=usb, expected_serial="TEST123"
    )
    repository = Repository(tmp_path)
    leases = ResourceLeases()
    sessions = SessionManager(leases, repository, lambda _: pytest.fail("Scan opened a model factory"))
    inventory = Inventory(repository)
    scanner = DiscoveryService(
        (ConfiguredDiscoveryProvider((config,)), discovery.UsbDiscoveryProvider()),
        sessions,
        leases,
        inventory,
        lambda _: pytest.fail("Scan probed an instrument"),
    )
    try:
        report = scanner.scan().result(timeout=3)
        assert report["items"][0]["errorCode"] == "driver_missing"
        (record,) = inventory.all()
        assert record["adapterId"] == "microchip.53100a" and record["driverStatus"] == "error"
        assert record["config"]["expected_serial"] == "TEST123"
        assert not record["identity"]
        assert record["candidates"][config.resource]["presenceState"] == "present"
    finally:
        scanner.close()
        sessions.close()
        repository.close()
