import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from lab_control.application.agents.policy import INTERNAL_CLIENT
from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceError, ValidationError


@pytest.fixture
def slots(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        devices = []
        for name in ("first", "second", "private"):
            config = ConnectionConfig(f"mock://slots/{name}", "rs.sma100b", "mock", expected_serial=name)
            device = runtime.add(config)
            runtime.connect(device).result(3)
            devices.append(device)
        service = runtime.agent
        client = INTERNAL_CLIENT
        grant = service.policy.create_grant(
            client, devices[:2], ["view"], channels=[1], operations=[], limits={}
        )
        custom = runtime.create_slot("signal_generator", "种子源 · 甲")
        runtime.bind_slot(devices[0], custom, "general")
        runtime.bind_slot(devices[2], "signal_generator:2", "general")
        runtime.bind_slot(devices[1], "signal_generator:1", "electronic")
        yield runtime, service, client, grant, devices, custom


def test_authorized_snapshot_filter_workspace_and_no_io(slots, monkeypatch):
    runtime, service, client, grant, devices, custom = slots

    def forbidden(*args, **kwargs):
        raise AssertionError("slot query attempted device I/O")

    monkeypatch.setattr(runtime, "connect", forbidden)
    monkeypatch.setattr(runtime.discovery, "scan", forbidden)
    monkeypatch.setattr(runtime.sessions, "submit", forbidden)
    for session in runtime.sessions.sessions.values():
        monkeypatch.setattr(session.adapter, "read_settings", forbidden)
        monkeypatch.setattr(session.adapter, "apply", forbidden)
    result = service.list_slots(client, grant)
    assert len(result) == 1
    item = result[0]
    assert item["slot_id"] == custom and item["label"] == "种子源 · 甲"
    assert item["device_id"] == devices[0] and item["cached"]
    assert "lastStatus" not in item["state"]
    assert service.list_slots(client, grant, category="signal_generator") == result
    assert service.list_slots(client, grant, category="oscilloscope") == []
    assert service.list_slots(client, grant, workspace="electronic")[0]["device_id"] == devices[1]
    assert service.resolve_slot(client, grant, custom) == item
    for unavailable in ("signal_generator:2", "signal_generator:missing"):
        with pytest.raises(DeviceError) as error:
            service.resolve_slot(client, grant, unavailable)
        assert error.value.code == "slot_unavailable"
    with pytest.raises(ValidationError):
        service.list_slots(client, grant, category="unknown")
    with pytest.raises(ValidationError):
        service.list_slots(client, grant, workspace="unknown")


def test_disconnect_rebind_and_delete_recreate(slots):
    runtime, service, client, grant, devices, custom = slots
    before = service.resolve_slot(client, grant, custom)
    runtime.sessions.disconnect(devices[0])
    disconnected = service.resolve_slot(client, grant, custom)
    assert disconnected["device_id"] == devices[0]
    assert disconnected["binding_fingerprint"] == before["binding_fingerprint"]
    runtime.bind_slot(devices[1], custom, "general", replace_device_id=devices[0])
    after = service.resolve_slot(client, grant, custom)
    assert after["device_id"] == devices[1]
    assert after["binding_fingerprint"] != before["binding_fingerprint"]
    runtime.bind_slot(devices[1], "", "general")
    runtime.delete_slot(custom)
    replacement = runtime.create_slot("signal_generator", "种子源 · 甲")
    assert replacement != custom
    with pytest.raises(DeviceError) as error:
        service.resolve_slot(client, grant, custom)
    assert error.value.code == "slot_unavailable"


@pytest.mark.parametrize("reason", ["unknown", "revoked", "legacy_client", "identity"])
def test_invalid_authority_never_returns_slot_metadata(slots, monkeypatch, reason):
    _runtime, service, client, grant, _devices, _custom = slots
    if reason == "unknown":
        grant = "unknown"
    elif reason == "revoked":
        service.policy.revoke_grant(grant)
    elif reason == "legacy_client":
        service.policy.grants[grant]["client_id"] = "legacy"
    else:
        monkeypatch.setattr(service.policy, "identity_lookup", lambda _device: {"serial": "changed"})
    with pytest.raises(DeviceError):
        service.list_slots(client, grant)


def test_snapshot_serializes_concurrent_binding_change(slots, monkeypatch):
    runtime, service, client, grant, devices, custom = slots
    entered, release, rebinding = threading.Event(), threading.Event(), threading.Event()
    original = runtime.repository.function_slots.definitions

    def held(category, mode):
        entered.set()
        assert release.wait(3)
        return original(category, mode)

    def rebind():
        rebinding.set()
        runtime.bind_slot(devices[1], custom, "general", replace_device_id=devices[0])

    monkeypatch.setattr(runtime.repository.function_slots, "definitions", held)
    with ThreadPoolExecutor(2) as workers:
        reading = workers.submit(service.resolve_slot, client, grant, custom)
        assert entered.wait(3)
        changing = workers.submit(rebind)
        try:
            assert rebinding.wait(3)
            assert not changing.done()
        finally:
            release.set()
        before = reading.result(3)
        changing.result(3)
    after = service.resolve_slot(client, grant, custom)
    assert before["device_id"] == devices[0] and after["device_id"] == devices[1]
    assert before["binding_fingerprint"] != after["binding_fingerprint"]
