import subprocess

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import DeviceError
from lab_control.instruments.devices.microchip_53100a import server, transport

USB = r"USB\VID_16D0&PID_05B8\TEST-USB"


class Process:
    pid = 9876
    returncode = None
    terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("owned-test-server", timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 1


@pytest.fixture
def setup_server(tmp_path, monkeypatch):
    from lab_control.deployment import windows

    executable = tmp_path / "tserve64.exe"
    executable.write_bytes(b"test")
    monkeypatch.setattr(server.sys, "platform", "win32")
    monkeypatch.setattr(server, "find_server", lambda _: executable)
    monkeypatch.setattr(
        server, "enumerate_devices", lambda: ({"PNPDeviceID": USB, "ConfigManagerErrorCode": 0},)
    )
    monkeypatch.setattr(server, "vendor_processes", lambda: [])
    monkeypatch.setattr(
        server, "verify_usb_identity", lambda *_: {"serial": "TEST123", "confidence": "usb_descriptor"}
    )
    monkeypatch.setattr(
        windows, "signature_status", lambda _: {"status": "Valid", "subject": "Miles Design LLC"}
    )
    monkeypatch.setattr(server, "application_home", lambda: tmp_path)
    process = Process()
    launches = []

    def launch(args, **kwargs):
        launches.append((args, kwargs))
        return process

    monkeypatch.setattr(server.subprocess, "Popen", launch)
    monkeypatch.setattr(server, "listener_pids", lambda _: {process.pid})
    config = ConnectionConfig(
        "USBPNP::" + USB, "microchip.53100a", "tserve", expected_serial="TEST123", usb_instance_id=USB
    )
    return server.ManagedServer(config), process, launches


def test_owned_lifecycle_never_starts_measurement(setup_server, monkeypatch):
    manager, process, launches = setup_server
    assert manager.start().startswith("tcp://127.0.0.1:")
    args, kwargs = launches[0]
    assert "/serial:TEST123" in args and f"/data_port:{manager.data_port}" in args and "/start" not in args
    assert manager.data_port != manager.port and manager.data_port > 0
    assert all(
        flag in args for flag in ("/format:P", "/timestamp:s", "/phasedec:2", "/sep:32", "/notify", "/ch:3-1")
    )
    assert kwargs["creationflags"] == 0x08000000
    commands = []

    class Control:
        def __init__(self, *_):
            pass

        def open(self):
            pass

        def write(self, command):
            commands.append(command)
            process.returncode = 0

        def close(self):
            pass

    monkeypatch.setattr(transport, "TserveTransport", Control)
    manager.close()
    assert commands == ["shutdown"] and not process.terminated and manager.log is None


def test_foreign_listener_is_never_given_shutdown(setup_server, monkeypatch):
    manager, process, launches = setup_server
    monkeypatch.setattr(server, "listener_pids", lambda _: {12345})
    monkeypatch.setattr(transport, "TserveTransport", lambda *_: pytest.fail("Contacted a foreign listener"))
    with pytest.raises(DeviceError):
        manager.start()
    assert len(launches) == 1 and process.terminated
    assert (manager.folder / "forced-exit.json").is_file()


@pytest.mark.parametrize(
    "processes",
    [
        [{"Name": "tserve64.exe", "CommandLine": "tserve64.exe /serial:TEST123"}],
        [{"Name": "tserve64.exe", "CommandLine": None}],
        [{"Name": "timelab64.exe", "CommandLine": "timelab64.exe"}],
    ],
)
def test_external_occupancy_blocks_before_launch(setup_server, monkeypatch, processes):
    manager, _, launches = setup_server
    monkeypatch.setattr(server, "vendor_processes", lambda: processes)
    with pytest.raises(DeviceError) as caught:
        manager.start()
    assert caught.value.code == "resource_busy" and launches == []


def test_signature_failure_never_launches(setup_server, monkeypatch):
    from lab_control.deployment import windows

    manager, _, launches = setup_server
    monkeypatch.setattr(windows, "signature_status", lambda _: {"status": "HashMismatch"})
    with pytest.raises(DeviceError, match="签名"):
        manager.start()
    assert not launches


def test_driver_missing_never_launches(setup_server, monkeypatch):
    manager, _, launches = setup_server
    monkeypatch.setattr(
        server, "enumerate_devices", lambda: ({"PNPDeviceID": USB, "ConfigManagerErrorCode": 28},)
    )
    with pytest.raises(DeviceError, match="驱动"):
        manager.start()
    assert not launches


def test_startup_timeout_cleans_only_owned_child_once(setup_server, monkeypatch):
    manager, process, launches = setup_server
    clock = iter((0, 1, 11))
    monkeypatch.setattr(server.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(server.time, "sleep", lambda _: None)
    monkeypatch.setattr(server, "listener_pids", lambda _: set())
    monkeypatch.setattr(transport, "TserveTransport", lambda *_: pytest.fail("No owned listener exists"))
    with pytest.raises(DeviceError) as caught:
        manager.start()
    assert isinstance(caught.value.__context__, DeviceError)
    assert caught.value.__context__.code == "timeout"
    assert caught.value.code == "server_exit_abnormal"
    assert len(launches) == 1 and process.terminated and manager.log is None


def test_usb_identity_mismatch_blocks_before_launch(setup_server, monkeypatch):
    manager, _, launches = setup_server

    def mismatch(*_):
        raise DeviceError("USB identity differs", "identity_conflict")

    monkeypatch.setattr(server, "verify_usb_identity", mismatch)
    with pytest.raises(DeviceError) as caught:
        manager.start()
    assert caught.value.code == "identity_conflict" and not launches
    assert manager.log is None


def test_external_adapter_close_does_not_manage_server(monkeypatch):
    from lab_control.instruments.devices.microchip_53100a import adapter
    from lab_control.transport.base import MockTransport

    wire = MockTransport([])
    monkeypatch.setattr(adapter, "TserveTransport", lambda *_, **__: wire)
    monkeypatch.setattr(server.ManagedServer, "start", lambda _: pytest.fail("Started external service"))
    client = adapter.create(
        ConnectionConfig("tcp://localhost:1299", "microchip.53100a", "tserve", identity_confirmed=True)
    )
    client.close()
    assert not wire.history
