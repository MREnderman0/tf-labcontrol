import json
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from lab_control.application.config import ConnectionConfig, canonical_resource, load_config
from lab_control.application.leases import ProcessLease, ResourceLeases
from lab_control.application.sessions import SessionManager
from lab_control.demo.instrument import SimulatedInstrument
from lab_control.discovery.service import (
    ConfiguredDiscoveryProvider,
    DiscoveryCandidate,
    DiscoveryService,
    Inventory,
    SerialDiscoveryProvider,
)
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError, DeviceIdentity
from lab_control.storage.repository import Repository


def test_twenty_sessions_scan_and_stream_are_protected(tmp_path):
    repo = Repository(tmp_path)
    leases = ResourceLeases()
    created = []

    def factory(config):
        device = SimulatedInstrument(config)
        created.append(device)
        return device

    sessions = SessionManager(leases, repo, factory)
    configs = tuple(ConnectionConfig(f"mock://{i}", "rigol.dg5504pro", "mock") for i in range(20))
    for index, config in enumerate(configs):
        sessions.connect(str(index), config).result()
    ids = {s["sessionId"] for s in sessions.snapshots()}
    inventory = Inventory(repo)

    def forbidden_probe(candidate):
        raise AssertionError("Scanner opened an existing session")

    scanner = DiscoveryService(
        (ConfiguredDiscoveryProvider(configs),), sessions, leases, inventory, forbidden_probe
    )
    ready, lock, producers = threading.Event(), threading.Lock(), set()

    def on_measurement(event):
        if event["event"] == "measurement":
            with lock:
                producers.add(event["device_id"])
                if len(producers) == 20:
                    ready.set()

    sessions.subscribe(on_measurement)
    try:
        acquisitions = [
            sessions.start_acquisition(
                str(i),
                AcquisitionRequest("waveform", points=32),
                interval=0.01,
                record_interval=0.5,
            )
            for i in range(20)
        ]
        assert ready.wait(5), "All 20 devices must produce data before scanning"
        for _ in range(3):
            result = scanner.scan().result()
            assert all(item["reason"] == "cached_active_session" for item in result["items"])
            assert {s["sessionId"] for s in sessions.snapshots()} == ids
            assert all(s["connectionState"] == "busy" for s in sessions.snapshots())
            assert len(created) == 20 and not any(device.closed for device in created)
        for index in range(20):
            sessions.stop_acquisition(str(index))
        assert all(acquisition.result() for acquisition in acquisitions)
    finally:
        scanner.close()
        sessions.close()
        repo.close()


def test_two_model_only_power_meters_can_use_distinct_resources(tmp_path):
    """A protocol without a serial query must not make separate ports collide."""
    repo = Repository(tmp_path)

    class ModelOnly(SimulatedInstrument):
        def identify(self):
            return DeviceIdentity("Joinwit", "JW8105", confidence="model_only")

    sessions = SessionManager(ResourceLeases(tmp_path), repo, ModelOnly)
    first = ConnectionConfig("COM11", "joinwit.jw8105", "serial", expected_serial="JW-A")
    second = ConnectionConfig("COM12", "joinwit.jw8105", "serial", expected_serial="JW-B")
    try:
        assert sessions.connect("meter-a", first).result(timeout=3).serial == ""
        assert sessions.connect("meter-b", second).result(timeout=3).serial == ""
        assert {item["connectionState"] for item in sessions.snapshots()} == {"connected"}
    finally:
        sessions.close()
        repo.close()


def test_runtime_can_configure_one_metadata_only_resource_before_connecting(tmp_path):
    from lab_control.application.runtime import ApplicationRuntime

    with ApplicationRuntime(root=tmp_path, hardware=False) as app:
        record = app.inventory.merge(
            DiscoveryCandidate(ConnectionConfig("mock://unknown", transport="mock"), "serial"),
            "scan",
        )
        app.configure_device(
            record["id"], ConnectionConfig("mock://unknown", "rigol.dg5504pro", "mock")
        )
        configured = next(item for item in app.inventory.all() if item["id"] == record["id"])
        assert configured["adapterId"] == "rigol.dg5504pro"
        assert configured["candidates"]["mock://unknown"]["source"] == "configured"
        assert app.connect(record["id"]).result(timeout=3).model == "DG5504 Pro"


def test_metadata_duplicate_does_not_block_explicit_configuration(tmp_path):
    repo = Repository(tmp_path)
    inventory = Inventory(repo)
    first = inventory.merge(
        DiscoveryCandidate(ConnectionConfig("COM7", transport="serial"), "serial"), "scan"
    )
    second = inventory.merge(
        DiscoveryCandidate(ConnectionConfig("ASRL7::INSTR", transport="serial"), "serial"), "scan"
    )
    assert first["id"] == second["id"]
    # Distinct persisted rows can exist after an old concurrent scan.  A
    # metadata-only duplicate must not prevent assigning the protocol.
    duplicate = {
        "id": "duplicate",
        "resources": ["COM7"],
        "candidates": {"COM7": {"retired": False}},
        "config": {**first["config"], "resource": "COM7"},
        "adapterId": "",
        "identity": {},
        "identityKey": None,
    }
    inventory.records[duplicate["id"]] = duplicate
    inventory.configure(second["id"], ConnectionConfig("COM7", "joinwit.jw8105", "serial"))
    assert inventory.records[second["id"]]["adapterId"] == "joinwit.jw8105"
    repo.close()


def test_serial_scan_uses_usb_bridge_identity_when_com_number_changes(tmp_path):
    repo = Repository(tmp_path)
    inventory = Inventory(repo)
    descriptor = {"vid": 1027, "pid": 24577, "usb_serial": "BRIDGE-A", "usb_instance_id": "VID_0403&PID_6001\\BRIDGE-A"}
    original = inventory.merge(
        DiscoveryCandidate(
            ConnectionConfig("COM7", transport="serial", usb_instance_id=descriptor["usb_instance_id"]),
            "serial",
            descriptor,
        ),
        "scan-1",
    )
    inventory.configure(original["id"], ConnectionConfig("COM7", "joinwit.jw8105", "serial"))
    moved = inventory.merge(
        DiscoveryCandidate(
            ConnectionConfig("COM12", transport="serial", usb_instance_id=descriptor["usb_instance_id"]),
            "serial",
            descriptor,
        ),
        "scan-2",
        reason="pending_probe",
    )
    assert moved["id"] == original["id"]
    assert moved["config"]["resource"] == "COM12"
    assert moved["config"]["adapter"] == "joinwit.jw8105"
    assert moved["primaryResourceExplicit"] is False
    repo.close()


def test_serial_provider_publishes_every_port_with_stable_usb_id(monkeypatch):
    import serial.tools.list_ports

    ports = (
        SimpleNamespace(device="COM7", description="JW", vid=1027, pid=24577, serial_number="A", location="1"),
        SimpleNamespace(device="COM9", description="JW", vid=1027, pid=24577, serial_number="B", location="2"),
        SimpleNamespace(device="COM3", description="Unknown", vid=None, pid=None, serial_number=None, location=None),
    )
    monkeypatch.setattr(serial.tools.list_ports, "comports", lambda: ports)
    candidates = SerialDiscoveryProvider().enumerate()
    assert [item.config.resource for item in candidates] == ["COM7", "COM9", "COM3"]
    assert candidates[0].config.usb_instance_id.endswith("\\A")
    assert candidates[1].config.usb_instance_id.endswith("\\B")
    assert candidates[2].config.usb_instance_id == ""


def test_configured_serial_model_is_scanned_on_each_matching_usb_bridge(tmp_path):
    repo = Repository(tmp_path)
    inventory = Inventory(repo)
    leases = ResourceLeases(tmp_path)
    sessions = SessionManager(leases, repo, SimulatedInstrument)
    descriptor_a = {"vid": 1027, "pid": 24577, "usb_serial": "A", "usb_instance_id": "VID_0403&PID_6001\\A"}
    descriptor_b = {"vid": 1027, "pid": 24577, "usb_serial": "B", "usb_instance_id": "VID_0403&PID_6001\\B"}
    first = inventory.merge(
        DiscoveryCandidate(
            ConnectionConfig("COM7", transport="serial", usb_instance_id=descriptor_a["usb_instance_id"]),
            "serial",
            descriptor_a,
        ),
        "initial",
    )
    inventory.configure(first["id"], ConnectionConfig("COM7", "joinwit.jw8105", "serial"))

    class Ports:
        name = "serial"

        def enumerate(self):
            return (
                DiscoveryCandidate(
                    ConnectionConfig("COM7", transport="serial", usb_instance_id=descriptor_a["usb_instance_id"]),
                    "serial",
                    descriptor_a,
                ),
                DiscoveryCandidate(
                    ConnectionConfig("COM9", transport="serial", usb_instance_id=descriptor_b["usb_instance_id"]),
                    "serial",
                    descriptor_b,
                ),
            )

    scanner = DiscoveryService(
        (Ports(),),
        sessions,
        leases,
        inventory,
        lambda _: (DeviceIdentity("Joinwit", "JW8105", confidence="model_only"), "joinwit.jw8105"),
    )
    try:
        result = scanner.scan().result(timeout=3)
        assert [item["reason"] for item in result["items"]] == ["identified", "identified"]
        current = [item for item in inventory.all() if item.get("presenceState") == "present"]
        assert {item["config"]["resource"] for item in current} == {"COM7", "COM9"}
        assert all(item["adapterId"] == "joinwit.jw8105" for item in current)
    finally:
        scanner.close()
        sessions.close()
        repo.close()


def test_runtime_never_crosses_real_and_mock_boundary(tmp_path):
    from lab_control.application.runtime import ApplicationRuntime

    with pytest.raises(DeviceError):
        ApplicationRuntime((ConnectionConfig("mock://one", "rigol.dg5504pro", "mock"),), root=tmp_path)
    with pytest.raises(DeviceError):
        ApplicationRuntime((ConnectionConfig("USB::one", "rigol.dg5504pro"),), root=tmp_path, hardware=False)


def test_atomic_alias_leases_and_process_mutex(tmp_path):
    leases = ResourceLeases()
    leases.acquire("first", ("COM4",))
    with pytest.raises(DeviceError):
        leases.acquire("second", ("ASRL4::INSTR", "unrelated"))
    assert leases.owner("unrelated") is None
    leases.release("first")
    assert canonical_resource("ASRL04::INSTR") == canonical_resource("COM4")
    first = ProcessLease(tmp_path)
    try:
        with pytest.raises(DeviceError):
            ProcessLease(tmp_path)
    finally:
        first.close()
    ProcessLease(tmp_path).close()


def test_provider_failure_cancellation_and_stale_preserve_inventory(tmp_path):
    repo = Repository(tmp_path)
    inventory = Inventory(repo)
    leases = ResourceLeases()
    sessions = SessionManager(leases, repo, SimulatedInstrument)
    config = ConnectionConfig("COM8", transport="serial")
    inventory.merge(DiscoveryCandidate(config, "serial"), "old")

    class Failed:
        name = "serial"

        def enumerate(self):
            raise RuntimeError("Port inventory failed")

    scanner = DiscoveryService(
        (Failed(), ConfiguredDiscoveryProvider((config,))),
        sessions,
        leases,
        inventory,
        lambda candidate: (_ for _ in ()).throw(AssertionError("Unknown COM probed")),
    )
    report = scanner.scan().result()
    assert report["providers"]["serial"]["state"] == "error"
    assert len(inventory.all()) == 1 and inventory.all()[0]["discoveryState"] != "stale"
    scanner.close()
    repo.close()


def test_identity_merge_preserves_addresses_and_conflict(tmp_path):
    repo = Repository(tmp_path)
    inventory = Inventory(repo)
    identity = DeviceIdentity("RIGOL", "DG5504 Pro", "SERIAL")
    first = inventory.merge(DiscoveryCandidate(ConnectionConfig("USB::1"), "visa"), "scan", identity)
    second = inventory.merge(DiscoveryCandidate(ConnectionConfig("TCPIP::host"), "visa"), "scan", identity)
    assert first["id"] == second["id"] and len(second["resources"]) == 2
    inventory.edit(first["id"], display_name="实验信号源")
    inventory.merge(
        DiscoveryCandidate(ConnectionConfig("USB::1"), "visa"), "scan2", replace(identity, serial="OTHER")
    )
    assert inventory.all()[0]["discoveryState"] == "conflict"
    repo.close()


def test_operation_serialization_and_busy_disconnect(tmp_path):
    repo, leases = Repository(tmp_path), ResourceLeases()
    log = []
    gate, entered = threading.Event(), threading.Event()

    class Slow(SimulatedInstrument):
        def apply(self, command):
            log.append(("start", command.parameters["value"]))
            entered.set()
            assert gate.wait(2)
            result = super().apply(command)
            log.append(("end", command.parameters["value"]))
            return result

    sessions = SessionManager(leases, repo, Slow)
    sessions.connect("one", ConnectionConfig("mock://one", "rigol.dg5504pro", "mock")).result()
    first = sessions.submit("one", "apply", DeviceCommand("frequency", {"value": 1}))
    assert entered.wait(1)
    second = sessions.submit("one", "apply", DeviceCommand("frequency", {"value": 2}))
    with pytest.raises(DeviceError):
        sessions.disconnect("one")
    gate.set()
    first.result()
    second.result()
    assert log == [("start", 1), ("end", 1), ("start", 2), ("end", 2)]
    sessions.close()
    repo.close()


def test_chinese_config_and_export(tmp_path):
    path = tmp_path / "设备配置.toml"
    path.write_text(
        '[[devices]]\nresource="mock://测试"\nadapter="rigol.dg5504pro"\ntransport="mock"\n', encoding="utf-8"
    )
    config = load_config(path)[0]
    repo = Repository(tmp_path / "实验记录")
    device = SimulatedInstrument(config)
    batch = device.acquire(AcquisitionRequest("waveform", points=20))
    target = repo.export(batch, "optical")
    assert "测试" in (target / "_raw" / "snapshot.json").read_text(encoding="utf-8")
    assert (target / "数据_00001.csv").read_text(encoding="utf-8-sig").startswith("帧号,点序号,")
    assert (
        json.loads((target / "_raw" / "snapshot.json").read_text(encoding="utf-8"))["measurements"][0]["quality"]
        == "simulated"
    )
    repo.close()


def test_discovery_publishes_all_candidates_before_probe_and_cancel_protects_leases(tmp_path):
    repo, leases = Repository(tmp_path), ResourceLeases()
    inventory = Inventory(repo)
    sessions = SessionManager(leases, repo, SimulatedInstrument)
    configs = tuple(ConnectionConfig(f"mock://{i}", "rigol.dg5504pro", "mock") for i in range(3))
    entered, release = threading.Event(), threading.Event()

    def probe(candidate):
        entered.set()
        assert release.wait(2)
        return DeviceIdentity("RIGOL", "DG5504 Pro", candidate.config.resource), candidate.config.adapter

    scanner = DiscoveryService((ConfiguredDiscoveryProvider(configs),), sessions, leases, inventory, probe)
    future = scanner.scan()
    assert entered.wait(1)
    assert len(inventory.all()) == 3
    with pytest.raises(DeviceError, match="scan"):
        scanner.scan()
    with pytest.raises(DeviceError, match="probe"):
        sessions.connect("racing", configs[0])
    scanner.cancel()
    release.set()
    result = future.result()
    assert result["state"] == "cancelled" and leases.owner(configs[0].resource) is None
    assert all(item["discoveryState"] != "stale" for item in inventory.all())
    scanner.close()
    sessions.close()
    repo.close()


def test_incremental_metadata_records_merge_after_identification(tmp_path):
    repo = Repository(tmp_path)
    inventory = Inventory(repo)
    a, b = (
        DiscoveryCandidate(ConnectionConfig("USB::1"), "visa"),
        DiscoveryCandidate(ConnectionConfig("TCPIP::host"), "visa"),
    )
    first = inventory.merge(a, "scan")
    inventory.merge(b, "scan")
    identity = DeviceIdentity("RIGOL", "DG5504 Pro", "SAME-SERIAL")
    inventory.merge(a, "scan", identity)
    merged = inventory.merge(b, "scan", identity)
    assert merged["id"] == first["id"] and len(inventory.all()) == 1
    assert len(repo.records()) == 1 and len(merged["candidates"]) == 2
    repo.close()
