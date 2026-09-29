import pytest

from lab_control.application.config import ConnectionConfig, load_config
from lab_control.domain.instruments import DeviceError
from lab_control.instruments.devices.microchip_53100a import discovery
from lab_control.instruments.devices.microchip_53100a.adapter import create
from lab_control.instruments.devices.microchip_53100a.server import ManagedServer

USB = r"USB\VID_16D0&PID_05B8\TEST-USB-ID"


def test_missing_driver_still_enumerates_without_starting_server(monkeypatch):
    monkeypatch.setattr(
        discovery,
        "enumerate_devices",
        lambda: ({"Name": "Microchip 53100A", "PNPDeviceID": USB, "ConfigManagerErrorCode": 28},),
    )
    monkeypatch.setattr(ManagedServer, "start", lambda _: pytest.fail("Scan started server"))
    (candidate,) = discovery.UsbDiscoveryProvider().enumerate()
    assert candidate.config.usb_instance_id == USB
    assert not candidate.config.expected_serial
    assert candidate.error_code == "driver_missing" and not candidate.safe_scpi


def test_existing_external_tserve_config_keeps_its_defaults(tmp_path):
    path = tmp_path / "devices.toml"
    path.write_text(
        '[[devices]]\nresource="tcp://localhost:1299"\nadapter="microchip.53100a"\ntransport="tserve"\nidentity_confirmed=true\n',
        encoding="utf-8",
    )
    (config,) = load_config(path)
    assert config.usb_instance_id == config.server_path == ""
    assert config.identity_confirmed


def test_local_server_requires_serial_before_pnp_or_launch(monkeypatch):
    from lab_control.instruments.devices.microchip_53100a import server

    monkeypatch.setattr(server.sys, "platform", "win32")
    monkeypatch.setattr(server, "enumerate_devices", lambda: pytest.fail("Unbound target enumerated"))
    with pytest.raises(DeviceError, match="序列号"):
        ManagedServer(
            ConnectionConfig("USBPNP::" + USB, "microchip.53100a", "tserve", usb_instance_id=USB)
        ).start()


def test_external_connection_still_requires_ownership_confirmation():
    with pytest.raises(DeviceError, match="ownership"):
        create(ConnectionConfig("tcp://localhost:1299", "microchip.53100a", "tserve"))


def test_wrong_transport_never_opens_hardware():
    with pytest.raises(DeviceError, match="not VISA"):
        create(ConnectionConfig("USB::TEST", "microchip.53100a"))
