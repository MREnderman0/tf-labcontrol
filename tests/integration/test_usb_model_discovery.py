import pytest

from lab_control.application.bulk_connection import device_label
from lab_control.application.config import ConnectionConfig
from lab_control.application.leases import ResourceLeases
from lab_control.application.sessions import SessionManager
from lab_control.discovery.service import (
    ConfiguredDiscoveryProvider,
    DiscoveryCandidate,
    DiscoveryService,
    Inventory,
)
from lab_control.domain.instruments import DeviceIdentity, jsonable
from lab_control.instruments.devices.microchip_53100a import discovery
from lab_control.storage.repository import Repository


@pytest.fixture
def phase_candidate(monkeypatch):
    usb = r"USB\VID_16D0&PID_05B8\SYNTHETIC-USB"
    monkeypatch.setattr(
        discovery, "enumerate_devices", lambda: ({"PNPDeviceID": usb, "ConfigManagerErrorCode": 0},)
    )
    (candidate,) = discovery.UsbDiscoveryProvider().enumerate()
    return candidate


def test_53100a_name_updates_after_connection(tmp_path, phase_candidate):
    repository = Repository(tmp_path)
    try:
        inventory = Inventory(repository)
        config = phase_candidate.config
        record = inventory.merge(phase_candidate, "scan", adapter=config.adapter)
        assert "身份待核验" in device_label(record)

        identity = DeviceIdentity("Microchip", "53100A", "TEST123", confidence="usb_descriptor")
        connected = inventory.merge(
            DiscoveryCandidate(config, "configured"), "connected", identity, config.adapter
        )
        assert connected["id"] == record["id"]
        assert device_label(connected) == "Microchip 53100A / 53100A / TEST123"
        assert (
            inventory.merge(phase_candidate, "rescan", adapter=config.adapter)["displayName"]
            == "Microchip 53100A"
        )
        assert repository.records()[0]["displayName"] == "Microchip 53100A"
    finally:
        repository.close()


@pytest.mark.parametrize(
    ("verified", "display_name", "expected_name"),
    [
        (True, "Microchip 53100A（身份待核验）", "Microchip 53100A"),
        (False, "Microchip 53100A（身份待核验）", "Microchip 53100A（身份待核验）"),
        (True, "实验室相噪仪（身份待核验）", "实验室相噪仪（身份待核验）"),
    ],
)
def test_53100a_old_name_migration(tmp_path, phase_candidate, verified, display_name, expected_name):
    repository = Repository(tmp_path)
    try:
        inventory = Inventory(repository)
        record = inventory.merge(phase_candidate, "scan", adapter=phase_candidate.config.adapter)
        if verified:
            identity = DeviceIdentity("Microchip", "53100A", "TEST123", confidence="usb_descriptor")
            record.update(identity=jsonable(identity), identityKey=identity.key, discoveryState="identified")
        record["displayName"] = display_name
        repository.save(record)
        (before,) = repository.records()

        (loaded,) = Inventory(repository).all()
        assert loaded["displayName"] == expected_name
        assert repository.records() == [{**before, "displayName": expected_name}]
    finally:
        repository.close()


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
