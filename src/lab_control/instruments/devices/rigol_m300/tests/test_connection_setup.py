"""Asset setup survives address changes without making discovery a write path."""

import re
import threading
from copy import deepcopy
from dataclasses import replace

import pytest

from lab_control.application.runtime import ApplicationRuntime
from lab_control.discovery.types import DiscoveryCandidate
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceError, DeviceIdentity
from lab_control.instruments.registry import build_scpi_adapter
from lab_control.transport.base import MockTransport

from ..protocol import scan_channels

CHANNELS = {ch: 5000 if ch <= 416 else 10000 for ch in range(401, 420)}


class DeviceState:
    def __init__(self):
        self.serial = "TEST-M300"
        self.firmware = "1.0"
        self.module_serial = "TEST-MUX"
        self.scan = ()
        self.monitor = "0"
        self.trigger = "IMM"
        self.closed_relays = 0
        self.functions = {ch: "VOLT" for ch in range(401, 433)}
        self.sensors = {ch: "TC" for ch in range(401, 433)}
        self.nominals = {ch: 10000 for ch in range(401, 433)}
        self.units = {ch: "C" for ch in range(401, 433)}
        self.writes = []
        self.fail_write = ""
        self.reset_on_read = False


class M300Transport(MockTransport):
    def __init__(self, state, resource="mock://m300"):
        super().__init__([])
        self.state, self.resource = state, resource
        self.open()

    def query(self, payload):
        self.history.append(payload)
        s = self.state
        if payload == "READ?" and s.reset_on_read:
            s.reset_on_read = False
            s.scan = ()
            s.functions = dict.fromkeys(s.functions, "VOLT")
            raise DeviceError("Simulated reboot during acquisition", "disconnected")
        fixed = {
            "*IDN?": f"RIGOL TECHNOLOGIES,M300,{s.serial},{s.firmware}",
            "INST:DMM:INST?": "1",
            "INST:DMM?": "1",
            "TRIG:SOUR?": s.trigger,
            "TRIG:COUN?": "1",
            "TRIG:TIM?": "10",
            "ROUT:MON:STAT?": s.monitor,
            "ROUT:SCAN?": "(@" + ",".join(map(str, s.scan)) + ")",
            "ROUT:SCAN:SIZE?": str(len(s.scan)),
            "SYST:ERR?": '0,"No error"',
            "READ?": ",".join(str(20 + (ch - 400) / 10) for ch in s.scan),
        }
        if payload in fixed:
            return fixed[payload].encode()
        if payload.startswith("SYST:CTYP?"):
            slot = int(payload.split()[-1])
            model = {400: "MC3132", 500: "MC3065"}.get(slot, "0")
            serial = s.module_serial if slot == 400 else "TEST-DMM" if slot == 500 else "0"
            return f"RIGOL TECHNOLOGIES,{model},{serial},1.0".encode()
        if payload.startswith("FORM:READ:"):
            return b"0"
        if payload.startswith("ROUT:CLOS?"):
            return ",".join([str(s.closed_relays)] + ["0"] * 31).encode()
        match = re.fullmatch(r"(\S+)\? \(@(\d+)\)", payload)
        assert match, payload
        command, ch = match[1], int(match[2])
        if command == "CONF":
            if s.functions[ch] == "TEMP":
                return f"TEMP {s.sensors[ch]},{s.nominals[ch]},+1.000000E+00,+3.000000E-07".encode()
            return f"{s.functions[ch]} +2.000000E+01,+6.000000E-06".encode()
        target = {
            "FUNC": s.functions,
            "TEMP:TRAN:TYPE": s.sensors,
            "TEMP:TRAN:THER:TYPE": s.nominals,
            "UNIT:TEMP": s.units,
        }
        return str(target[command][ch]).encode()

    def write(self, payload):
        self.history.append(payload)
        s = self.state
        s.writes.append(payload)
        if payload.startswith("ROUT:SCAN "):
            s.scan = scan_channels(payload.split(" ", 1)[1])
        else:
            match = re.fullmatch(r"(\S+) ([^,]+),\(@(\d+)\)", payload)
            assert match, payload
            command, value, ch = match[1], match[2].strip('"'), int(match[3])
            target = {
                "FUNC": s.functions,
                "TEMP:TRAN:TYPE": s.sensors,
                "TEMP:TRAN:THER:TYPE": s.nominals,
                "UNIT:TEMP": s.units,
            }
            target[command][ch] = int(value) if command == "TEMP:TRAN:THER:TYPE" else value
        if s.fail_write and s.fail_write in payload:
            raise DeviceError("timeout after instrument accepted write", "timeout")


def bound_adapter(state=None, saved=None, save=None):
    state = state or DeviceState()
    transport = M300Transport(state)
    adapter = build_scpi_adapter(ConnectionConfig(transport.resource, "rigol.m300", "mock"), transport)
    adapter.identify()
    storage = {"value": deepcopy(saved)}

    def store(value):
        storage["value"] = deepcopy(value)

    adapter.bind_control_state(lambda: deepcopy(storage["value"]), save or store)
    return adapter, transport, storage


def test_remember_is_query_only_and_connection_restores_full_bank_once():
    adapter, transport, storage = bound_adapter()
    adapter.remember_thermistor_setup(CHANNELS, confirmed=True)
    assert storage["value"]["identity_key"].endswith("|TEST-M300")
    assert not transport.state.writes and "READ?" not in transport.history
    original = deepcopy(transport.state.functions)
    adapter.prepare_acquisition()
    state = transport.state
    assert state.scan == tuple(CHANNELS)
    assert all(state.functions[ch] == "TEMP" and state.sensors[ch] == "THER" for ch in CHANNELS)
    assert all(state.nominals[ch] == nominal and state.units[ch] == "C" for ch, nominal in CHANNELS.items())
    assert all(state.functions[ch] == original[ch] for ch in range(420, 433))
    assert state.writes[-1].startswith("ROUT:SCAN ")
    assert len(state.writes) == 19 + 19 + 16 + 1
    assert "READ?" not in transport.history
    attempt = storage["value"]["last_attempt"]
    assert attempt["before"]["settings"]["scan_list"] == "(@)"
    assert attempt["before"]["inputs"]["401"]["function"] == "VOLT"
    assert attempt["latent_before"]["401"] == {"sensor": "TC", "unit": "C", "nominal": 10000}
    assert attempt["status"] == "ready" and attempt["after"]["settings"]["scan_size"] == 19
    state.writes.clear()
    adapter.prepare_acquisition()
    assert not state.writes
    batch = adapter.acquire(AcquisitionRequest("scalar", tuple(CHANNELS), confirmed=True))
    assert len(batch.measurements) == 19 and all(m.unit == "°C" for m in batch.measurements)


def test_no_saved_or_disabled_profile_never_initializes_device():
    adapter, transport, storage = bound_adapter()
    adapter.prepare_acquisition()
    assert transport.history == ["*IDN?"]
    adapter.remember_thermistor_setup(CHANNELS, confirmed=True)
    adapter.disable_thermistor_setup(confirmed=True)
    history = list(transport.history)
    adapter.prepare_acquisition()
    assert transport.history == history and storage["value"]["enabled"] is False


@pytest.mark.parametrize("change", ["identity", "firmware", "module", "monitor", "trigger", "relays", "scan"])
def test_incompatible_identity_or_active_state_is_blocked_before_writes(change):
    adapter, transport, storage = bound_adapter()
    adapter.remember_thermistor_setup(CHANNELS, confirmed=True)
    state = transport.state
    if change == "identity":
        adapter.identity = replace(adapter.identity, serial="ANOTHER-M300")
    elif change == "firmware":
        adapter.identity = replace(adapter.identity, firmware="2.0")
    elif change == "module":
        state.module_serial = "REPLACED-MUX"
    elif change == "monitor":
        state.monitor = "1"
    elif change == "trigger":
        state.trigger = "EXT"
    elif change == "relays":
        state.closed_relays = 1
    else:
        state.scan = (432,)
    with pytest.raises(DeviceError):
        adapter.prepare_acquisition()
    assert not state.writes and "READ?" not in transport.history


def test_uncertain_write_is_journaled_and_blocked_across_reconnect():
    adapter, transport, storage = bound_adapter()
    adapter.remember_thermistor_setup(CHANNELS, confirmed=True)
    transport.state.fail_write = 'FUNC "TEMP",(@401)'
    with pytest.raises(DeviceError, match="未自动重试"):
        adapter.prepare_acquisition()
    assert transport.state.writes == ['FUNC "TEMP",(@401)']
    assert storage["value"]["last_attempt"]["status"] == "failed"
    again, again_transport, _ = bound_adapter(transport.state, storage["value"])
    with pytest.raises(DeviceError, match="禁止自动重放"):
        again.prepare_acquisition()
    assert again_transport.history == ["*IDN?"]
    assert transport.state.writes == ['FUNC "TEMP",(@401)']


def test_storage_failure_blocks_first_setting_write():
    adapter, transport, storage = bound_adapter()
    adapter.remember_thermistor_setup(CHANNELS, confirmed=True)

    def fail(_value):
        raise OSError("disk full")

    adapter.connection_setup.save = fail
    with pytest.raises(DeviceError, match="disk full"):
        adapter.prepare_acquisition()
    assert not transport.state.writes


@pytest.mark.parametrize(
    "channels,unit", [({}, "C"), ({401: 1234}, "C"), ({499: 5000}, "C"), ({401: 5000}, "X")]
)
def test_invalid_or_unconfirmed_profiles_never_write(channels, unit):
    adapter, transport, storage = bound_adapter()
    with pytest.raises(DeviceError):
        adapter.remember_thermistor_setup(channels, unit=unit, confirmed=True)
    with pytest.raises(DeviceError, match="授权"):
        adapter.remember_thermistor_setup(CHANNELS)
    assert not transport.state.writes and storage["value"] is None


def attach_factory(runtime, state, transports):
    def factory(config):
        transport = M300Transport(state, config.resource)
        transports.append(transport)
        return build_scpi_adapter(config, transport)

    runtime.sessions.factory = factory


def test_restart_new_address_retains_asset_setup_and_readonly_connection_skips_it(tmp_path):
    config = ConnectionConfig(
        "TCPIP0::192.0.2.10::inst0::INSTR", "rigol.m300", "mock", expected_serial="TEST-M300"
    )
    state, transports = DeviceState(), []
    app = ApplicationRuntime(root=tmp_path, hardware=False)
    attach_factory(app, state, transports)
    try:
        device = app.add(config)
        app.connect(device, configure_acquisition=False).result(5)
        session = app.sessions.sessions[device]
        session.worker.submit(
            lambda: session.adapter.remember_thermistor_setup(CHANNELS, confirmed=True)
        ).result(5)
        saved = app.repository.load_workspace(f"device-control-state/{device}")
        assert saved["channels"] == {str(ch): value for ch, value in CHANNELS.items()}
        app.inventory.select_resource(device, config.resource, follow_address=True)
    finally:
        app.close()
    assert not state.writes

    app = ApplicationRuntime(root=tmp_path, hardware=False)
    attach_factory(app, state, transports)
    try:
        changed = replace(config, resource="TCPIP0::192.0.2.88::inst0::INSTR")
        identity = DeviceIdentity("RIGOL TECHNOLOGIES", "M300", state.serial, state.firmware)
        record = app.inventory.merge(
            DiscoveryCandidate(changed, "lan", safe_scpi=True),
            "new-address",
            identity,
            "rigol.m300",
            reason="identified",
        )
        assert record["id"] == device and record["config"]["resource"] == changed.resource
        assert not state.writes
        app.connect(device, configure_acquisition=False).result(5)
        assert (
            not state.writes
            and app.sessions.capability_snapshot(device).details["acquisition_channels"] == []
        )
        app.sessions.disconnect(device)
        app.connect(device).result(5)
        assert state.scan == tuple(CHANNELS)
        assert app.sessions.capability_snapshot(device).details["acquisition_channels"] == list(CHANNELS)
        assert app.sessions.sessions[device].config.resource == changed.resource
        received = threading.Event()
        app.monitoring.subscribe_frames(lambda _device: received.set())
        app.monitoring.start(
            device, (AcquisitionRequest("scalar", tuple(CHANNELS), confirmed=True),), once=True
        )
        assert received.wait(5)
        frame = app.monitoring.latest(device, "scalar")
        assert len(frame.batch.measurements) == 19
        assert all(m.unit == "°C" for m in frame.batch.measurements)
        app.monitoring.stop(device).result(5)
        app.sessions.disconnect(device)
        writes = list(state.writes)
        app.connect(device).result(5)
        assert state.writes == writes
    finally:
        app.close()


def test_wrong_serial_at_saved_address_never_receives_remembered_settings(tmp_path):
    config = ConnectionConfig("mock://m300", "rigol.m300", "mock", expected_serial="TEST-M300")
    state, transports = DeviceState(), []
    app = ApplicationRuntime(root=tmp_path, hardware=False)
    attach_factory(app, state, transports)
    try:
        device = app.add(config)
        app.connect(device, configure_acquisition=False).result(5)
        session = app.sessions.sessions[device]
        session.worker.submit(
            lambda: session.adapter.remember_thermistor_setup(CHANNELS, confirmed=True)
        ).result(5)
        app.sessions.disconnect(device)
        state.serial = "OTHER-M300"
        with pytest.raises(DeviceError, match="serial differs"):
            app.connect(device).result(5)
        assert not state.writes and state.scan == ()
    finally:
        app.close()


def test_crash_journal_prevents_replaying_accepted_write_after_storage_failure():
    adapter, transport, storage = bound_adapter()
    adapter.remember_thermistor_setup(CHANNELS, confirmed=True)
    original_save = adapter.connection_setup.save

    def fail_after_write(value):
        if transport.state.writes:
            raise OSError("disk full after write")
        original_save(value)

    adapter.connection_setup.save = fail_after_write
    with pytest.raises(DeviceError, match="disk full after write"):
        adapter.prepare_acquisition()
    assert len(transport.state.writes) == 1
    assert storage["value"]["last_attempt"]["status"] == "writing"
    again, _, _ = bound_adapter(transport.state, storage["value"])
    with pytest.raises(DeviceError, match="禁止自动重放"):
        again.prepare_acquisition()
    assert len(transport.state.writes) == 1


def test_monitor_recovery_restores_authorized_profile_after_simulated_reboot(tmp_path):
    config = ConnectionConfig("mock://reboot", "rigol.m300", "mock", expected_serial="TEST-M300")
    state, transports = DeviceState(), []
    app = ApplicationRuntime(root=tmp_path, hardware=False)
    attach_factory(app, state, transports)
    try:
        device = app.add(config)
        app.connect(device, configure_acquisition=False).result(5)
        session = app.sessions.sessions[device]
        session.worker.submit(
            lambda: session.adapter.remember_thermistor_setup(CHANNELS, confirmed=True)
        ).result(5)
        app.sessions.disconnect(device)
        app.connect(device).result(5)
        state.writes.clear()
        state.reset_on_read = True
        received = threading.Event()
        recovered = []

        def accept(_device):
            frame = app.monitoring.latest(device, "scalar")
            if frame and "_acquisition_gap" in frame.batch.configuration:
                recovered.append(frame)
                received.set()

        app.monitoring.subscribe_frames(accept)
        app.monitoring.start(
            device, (AcquisitionRequest("scalar", tuple(CHANNELS), confirmed=True),), interval=0.05
        )
        assert received.wait(5)
        app.monitoring.stop(device).result(5)
        assert len(recovered) == 1 and len(recovered[0].batch.measurements) == 19
        assert all(m.unit == "°C" for m in recovered[0].batch.measurements)
        assert state.scan == tuple(CHANNELS)
        assert len(state.writes) == 20  # Lost functions and scan list; retained thermistor settings.
        assert len(transports) == 3  # Read-only registration, formal connection, automatic recovery.
    finally:
        app.close()
