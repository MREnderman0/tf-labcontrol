from dataclasses import replace
from types import SimpleNamespace

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.discovery.lan import LanDiscoverySettings, RememberedLanDiscoveryProvider
from lab_control.discovery.presence import is_current
from lab_control.discovery.service import DiscoveryCandidate
from lab_control.domain.instruments import DeviceIdentity
from lab_control.instruments.devices.ceyear_6362d import authentication
from lab_control.instruments.registry import DEVICE_DEFINITIONS

RESOURCE = "TCPIP0::192.0.2.10::10001::SOCKET"


class LoginInstrument:
    def __init__(self, serial="FIXTURE-A", challenge=b"AUTHENTICATE CRAM-MD5.", ready=b"ready"):
        self.serial = serial
        self.challenge = challenge
        self.ready = ready
        self.events = []

    def open(self):
        self.events.append("open")

    def query(self, command):
        self.events.append(command)
        if command == 'open "anonymous"':
            return self.challenge
        assert command == "*IDN?"
        assert self.events[-2] == "authenticated"
        return f"Ceyear Technologies,CY6362D,{self.serial},2.2.1".encode()

    def write(self, payload):
        assert payload == b"fixture-password\n"
        self.events.append("credential_sent")

    def read(self):
        self.events.append("authenticated")
        return self.ready

    def close(self):
        self.events.append("close")


@pytest.fixture
def saved_runtime(tmp_path, monkeypatch):
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    config = ConnectionConfig(
        RESOURCE,
        "ceyear.6362d",
        expected_serial="FIXTURE-A",
        timeout=7,
        limits={"fixture": 1.0},
        options=("fixture-option",),
    )
    identity = DeviceIdentity("Ceyear Technologies", "CY6362D", "FIXTURE-A", "2.2.1")
    record = runtime.inventory.merge(
        DiscoveryCandidate(config, "configured"), "old", identity, config.adapter
    )
    runtime.close()
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    assert not is_current(runtime.inventory.all()[0])
    runtime.discovery.providers = (
        RememberedLanDiscoveryProvider(
            runtime.inventory, LanDiscoverySettings(enabled=True, networks=("192.0.2.0/24",))
        ),
    )
    monkeypatch.setattr(authentication, "load_credentials", lambda _: ("anonymous", "fixture-password"))
    yield runtime, record["id"]
    runtime.close()


def install_transport(monkeypatch, instrument):
    configurations = []

    def transport(config, _visa):
        configurations.append(config)
        return instrument

    monkeypatch.setattr("lab_control.application.runtime.transport_for", transport)
    return configurations


def test_remembered_scan_authenticates_after_restart_without_initializing_format(saved_runtime, monkeypatch):
    runtime, original_id = saved_runtime
    instrument = LoginInstrument()
    configurations = install_transport(monkeypatch, instrument)
    for _ in range(3):
        report = runtime.discovery.scan().result(5)
        assert report["items"][0]["reason"] == "identified"
    assert (
        instrument.events
        == ["open", 'open "anonymous"', "credential_sent", "authenticated", "*IDN?", "close"] * 3
    )
    assert all(
        not c.adapter and not c.expected_serial and not c.limits and not c.options for c in configurations
    )
    assert all(c.timeout == 7 for c in configurations)
    current = runtime.inventory.all()[0]
    assert is_current(current) and current["id"] == original_id
    assert current["config"]["limits"] == {"fixture": 1.0}
    assert not runtime.sessions.sessions


def test_address_reuse_reads_new_identity_without_transferring_old_settings(saved_runtime, monkeypatch):
    runtime, original_id = saved_runtime
    install_transport(monkeypatch, LoginInstrument(serial="FIXTURE-B"))
    runtime.discovery.scan().result(5)
    records = runtime.inventory.all()
    old = next(r for r in records if r["id"] == original_id)
    new = next(r for r in records if r["identity"]["serial"] == "FIXTURE-B")
    assert not is_current(old) and old["candidates"][RESOURCE]["retired"]
    assert is_current(new) and new["id"] != original_id
    assert new["config"]["limits"] == {} and not new["config"]["options"]
    assert new["config"]["expected_serial"] == "FIXTURE-B"


@pytest.mark.parametrize(
    "challenge,ready,credential_sent",
    [
        (b"other service", b"ready", False),
        (b"AUTHENTICATE CRAM-MD5", b"denied", True),
    ],
)
def test_authentication_failure_is_offline_without_id_query_or_retry(
    saved_runtime, monkeypatch, challenge, ready, credential_sent
):
    runtime, _ = saved_runtime
    instrument = LoginInstrument(challenge=challenge, ready=ready)
    install_transport(monkeypatch, instrument)
    report = runtime.discovery.scan().result(5)
    assert report["items"][0]["reason"] == "probe_failed"
    assert not is_current(runtime.inventory.all()[0])
    assert "*IDN?" not in instrument.events
    assert ("credential_sent" in instrument.events) == credential_sent
    assert instrument.events.count("open") == 1 and instrument.events[-1] == "close"
    assert "fixture-password" not in str(report)


def test_broadcast_duplicate_keeps_model_handshake(saved_runtime, monkeypatch):
    runtime, _ = saved_runtime
    instrument = LoginInstrument()
    install_transport(monkeypatch, instrument)
    announcement = DiscoveryCandidate(ConnectionConfig(RESOURCE), "lan", safe_scpi=True)
    runtime.discovery.providers = (
        SimpleNamespace(name="lan", enumerate=lambda: (announcement,)),
        *runtime.discovery.providers,
    )
    report = runtime.discovery.scan().result(5)
    assert len(report["items"]) == 1 and instrument.events.count("*IDN?") == 1
    assert is_current(runtime.inventory.all()[0])


@pytest.mark.parametrize("kind", ["unverified", "retired", "conflict", "removed"])
def test_untrusted_or_removed_model_does_not_receive_saved_credentials(saved_runtime, monkeypatch, kind):
    runtime, _ = saved_runtime
    record = runtime.inventory.by_resource(RESOURCE)
    if kind == "unverified":
        record["candidates"][RESOURCE].pop("verifiedIdentityKey")
    elif kind == "retired":
        record["candidates"][RESOURCE]["retired"] = True
    elif kind == "conflict":
        record["identityConflict"] = {"reason": "fixture"}
    else:
        monkeypatch.delitem(DEVICE_DEFINITIONS, "ceyear.6362d")
    instrument = LoginInstrument()
    install_transport(monkeypatch, instrument)
    with pytest.raises(AssertionError):
        runtime.probe(DiscoveryCandidate(ConnectionConfig(RESOURCE), "lan", safe_scpi=True))
    assert instrument.events == ["open", "*IDN?", "close"]


def test_hook_receives_fresh_configuration_only(saved_runtime, monkeypatch):
    runtime, _ = saved_runtime
    original = DEVICE_DEFINITIONS["ceyear.6362d"]
    configs = []

    def read_identity(transport, config):
        configs.append(config)
        return authentication.discovery_identity(transport, config)

    monkeypatch.setitem(
        DEVICE_DEFINITIONS, original.adapter_id, replace(original, discovery_identity=read_identity)
    )
    install_transport(monkeypatch, LoginInstrument())
    runtime.discovery.scan().result(5)
    assert len(configs) == 1 and configs[0] == ConnectionConfig(RESOURCE, timeout=7)
