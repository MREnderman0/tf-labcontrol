import threading

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceCommand, DeviceError
from lab_control.instruments.registry import build_scpi_adapter
from lab_control.transport.base import MockTransport
from lab_control.transport.visa import VisaTransport

from ..adapter import Gpd4303sAdapter, gpd4303s
from ..hardware.validate_control import TestSession as HardwareSession
from ..hardware.validate_control import validate_restoration_target
from ..protocol import PacedTransport, decode_status, numeric_readback


@pytest.mark.parametrize(
    "raw,unit,expected",
    [(b"12.000V\r\n", "V", 12), (b"0.100A\r\n", "A", 0.1), (b"0.000V\r\n", "V", 0), (b"1e-3", "A", 0.001)],
)
def test_v212_numeric_units(raw, unit, expected):
    assert numeric_readback(raw, unit=unit) == expected


@pytest.mark.parametrize("raw", [b"1A", b"NaN", b"INF", b"12V trailing", b"1\n2V"])
def test_invalid_numeric_unit_or_value_is_rejected(raw):
    with pytest.raises(DeviceError):
        numeric_readback(raw, unit="V")


def device(exchanges):
    wire = MockTransport(exchanges)
    wire.open()
    config = ConnectionConfig("mock://gpd", "gwinstek.gpd4303s", "mock")
    adapter = build_scpi_adapter(config, wire)
    adapter.transport.interval = 0
    return adapter, wire


def refresh(voltage="2", current="0.1", status=b"\x87"):
    return [("STATUS?", status), ("VSET3?", voltage.encode()), ("ISET3?", current.encode())]


@pytest.mark.parametrize("value", range(256))
def test_all_status_bytes_preserve_payload(value):
    status = decode_status(bytes([value]))
    assert status["raw"] == value and status["raw_hex"] == f"{value:02x}"
    assert status["output_enabled"] == bool(value & 32)
    assert status["tracking"] == {1: "0", 2: "2", 3: "1"}.get((value >> 2) & 3)


@pytest.mark.parametrize("raw", [b"", b"1\n", b"ON", b"\x87\r\n"])
def test_invalid_status_length_is_not_a_false_output(raw):
    with pytest.raises(DeviceError):
        decode_status(raw)


@pytest.mark.parametrize(
    "raw,tracking,output",
    [(b"00011010\r\n", "0", False), (b"11111110\r\n", "1", True), (b"11101010\r\n", "2", False)],
)
def test_v212_ascii_fields_are_not_an_msb_first_byte(raw, tracking, output):
    status = decode_status(raw)
    assert status["tracking"] == tracking and status["output_enabled"] is output
    assert status["baud_rate"] == 9600 and status["encoding"] == "ascii_fields"
    assert status["raw_hex"] == raw.hex()


def test_ascii_status_framing_followed_by_numeric_query():
    visa = VisaTransport(None, "ASRL9::INSTR")
    visa.handle = SerialHandle()
    visa.handle.responses = iter([b"00011010\r\n", b"2.000\r\n"])
    paced = PacedTransport(visa, interval=0)
    assert decode_status(paced.query_status())["tracking"] == "0"
    assert paced.query("VSET1?") == b"2.000\r\n"


def test_panel_verified_zero_tracking_variant_is_limited_to_v212_ascii():
    raw = b"00001010\r\n"
    actual = decode_status(raw, firmware="V2.12")
    assert actual["tracking"] == "0" and actual["tracking_bits"] == 0
    assert actual["raw_hex"] == raw.hex()
    assert actual["tracking_source"] == "v2.12_panel_and_track0"
    for firmware in ("", "V2.11", "V2.13"):
        assert decode_status(raw, firmware=firmware)["tracking"] is None
    assert decode_status(b"\x90", firmware="V2.12")["tracking"] is None


@pytest.mark.parametrize(
    "voltage,current,vmax,imax", [(5, 3, 5, 3), (5.001, 1, 10, 1), (10, 0.1, 10, 1), (2, 0.1, 10, 3)]
)
def test_channel_limits_and_no_cross_channel_leak(voltage, current, vmax, imax):
    adapter, _ = device(refresh(str(voltage), str(current)))
    adapter.refresh_capabilities()
    cap = adapter.capabilities_for_channel(3)
    assert cap.get("voltage").parameters["value"].maximum == vmax
    assert cap.get("current").parameters["value"].maximum == imax
    assert adapter.capabilities_for_channel(1).get("voltage").parameters["value"].maximum == 30
    assert adapter.capabilities_for_channel(4).get("current").parameters["value"].maximum == 1


def test_fresh_correlated_read_rejects_invalid_combination_without_setting_write():
    adapter, wire = device(refresh("6", "1"))
    with pytest.raises(DeviceError):
        adapter.apply(DeviceCommand("current", {"value": 2}, 3))
    assert all(str(command).endswith("?") for command in wire.history)


def test_stale_read_retains_limits_and_blocks_writes():
    adapter, wire = device(refresh("6", "1") + [("STATUS?", DeviceError("timeout", "timeout"))])
    adapter.refresh_capabilities()
    with pytest.raises(DeviceError):
        adapter.apply(DeviceCommand("current", {"value": 0.5}, 3))
    cap = adapter.capabilities_for_channel(3).get("current")
    assert cap.parameters["value"].maximum == 1 and not cap.writable
    assert all(str(command).endswith("?") for command in wire.history)


@pytest.mark.parametrize(
    "command",
    [
        DeviceCommand("voltage", {"value": 10.001}, 3),
        DeviceCommand("current", {"value": 1.001}, 4),
        DeviceCommand("voltage", {"value": float("nan")}),
        DeviceCommand("voltage", {"value": 1.0001}),
        DeviceCommand("all_outputs", {"value": True}),
        DeviceCommand("all_outputs", {"value": True}, 2, True),
    ],
)
def test_invalid_commands_and_confirmation_rejected_before_io(command):
    adapter, wire = device([])
    with pytest.raises(DeviceError):
        adapter.apply(command)
    assert wire.history == []


def test_master_output_verifies_binary_state_and_never_invents_channel_commands():
    adapter, wire = device([("STATUS?", b"\x87"), ("OUT1", b""), ("STATUS?", b"\xa7")])
    result = adapter.apply(DeviceCommand("all_outputs", {"value": True}, confirmed=True))
    assert result.outcome == "success" and result.readback is True
    assert wire.history == ["STATUS?", "OUT1", "STATUS?"]
    assert "output" not in gpd4303s().commands


def test_undocumented_mode_blocks_tracking_channels_and_output_enable_but_allows_off_and_mode():
    adapter, wire = device(refresh(status=b"00001010\r\n"))
    adapter.refresh_capabilities()
    assert adapter.status["tracking"] is None
    for ch in (1, 2):
        assert not adapter.capabilities_for_channel(ch).get("voltage").writable
    adapter, wire = device([("STATUS?", b"00001010\r\n")])
    with pytest.raises(DeviceError):
        adapter.apply(DeviceCommand("all_outputs", {"value": True}, confirmed=True))
    assert wire.history == ["STATUS?"]
    adapter, wire = device(
        [
            ("OUT0", b""),
            ("STATUS?", b"00001010\r\n"),
            ("STATUS?", b"00001010\r\n"),
            ("TRACK0", b""),
            ("STATUS?", b"00011010\r\n"),
        ]
    )
    assert adapter.apply(DeviceCommand("all_outputs", {"value": False}, confirmed=True)).outcome == "success"
    assert adapter.apply(DeviceCommand("tracking", {"value": "0"}, confirmed=True)).outcome == "success"


def test_panel_evidence_does_not_rewrite_raw_snapshot():
    from ..hardware.validate_panel import panel_restore_target

    raw = {"status": {"tracking": None, "tracking_bits": 0, "output_enabled": False}}
    with pytest.raises(DeviceError):
        panel_restore_target(raw, None)
    target = panel_restore_target(
        raw, {"tracking": "0", "source": "user_panel_photo", "photo_sha256": "test"}
    )
    assert target["status"]["tracking"] == "0"
    assert raw["status"]["tracking"] is None


def test_tracking_requires_output_off_and_restricts_slave_controls():
    adapter, wire = device([("STATUS?", b"\xa7")])
    with pytest.raises(DeviceError):
        adapter.apply(DeviceCommand("tracking", {"value": "1"}, confirmed=True))
    assert wire.history == ["STATUS?"]
    adapter, _ = device(refresh(status=b"\x8f") + refresh(status=b"\x8b"))
    adapter.refresh_capabilities()
    assert not adapter.capabilities_for_channel(2).get("voltage").writable
    assert adapter.capabilities_for_channel(2).get("current").writable
    adapter.refresh_capabilities()
    assert not adapter.capabilities_for_channel(2).get("current").writable


def test_write_timeout_is_not_replayed():
    adapter, wire = device(refresh() + [("VSET1:1.000", b""), ("VSET1?", DeviceError("timeout", "timeout"))])
    result = adapter.apply(DeviceCommand("voltage", {"value": 1}, 1))
    assert result.outcome == "partial" and result.completed_steps == 1
    assert wire.history.count("VSET1:1.000") == 1


class SerialHandle:
    def __init__(self):
        self.read_termination = "\n"
        self.data = bytearray()
        self.responses = iter([b"\x0a\r\n", b"\x0d\n", b"2.000\r\n", b"\x87"])

    @property
    def bytes_in_buffer(self):
        return len(self.data)

    def write(self, payload):
        assert not self.data
        self.data.extend(next(self.responses))

    def read_bytes(self, count, break_on_termchar=False):
        assert self.read_termination is None
        result = bytes(self.data[:count])
        del self.data[:count]
        return result


def test_fixed_and_text_framing_consumes_only_old_suffixes():
    visa = VisaTransport(None, "ASRL9::INSTR")
    visa.handle = SerialHandle()
    paced = PacedTransport(visa, interval=0)
    assert paced.query_fixed("STATUS?", 1) == b"\x0a"
    assert paced.query_fixed("STATUS?", 1) == b"\x0d"
    assert paced.query("VSET1?") == b"2.000\r\n"
    assert paced.query_fixed("STATUS?", 1) == b"\x87"
    assert visa.handle.read_termination == "\n"


class SupplyFixture:
    resource = "mock://gpd"

    def __init__(self):
        self.voltage = {1: 3.0, 2: 4.0, 3: 5.0, 4: 2.0}
        self.current = {ch: 0.2 for ch in range(1, 5)}
        self.output, self.tracking = False, "0"
        self.history = []
        self.threads = set()
        self.fail_on = ""

    def open(self):
        pass

    def close(self):
        pass

    def read(self):
        raise AssertionError("Use query")

    def write(self, payload):
        self.threads.add(threading.get_ident())
        self.history.append(payload)
        if payload == self.fail_on:
            raise DeviceError("injected failure", "timeout")
        if payload.startswith("OUT"):
            self.output = payload == "OUT1"
        elif payload.startswith("TRACK"):
            self.tracking = payload[-1]
        elif payload.startswith(("VSET", "ISET")):
            values = self.voltage if payload.startswith("V") else self.current
            values[int(payload[4])] = float(payload.split(":")[1])

    def query(self, payload):
        self.threads.add(threading.get_ident())
        self.history.append(payload)
        if payload == "*IDN?":
            return b"GW INSTEK,GPD-4303S,SN:TEST,V2.12"
        if payload == "STATUS?":
            return bytes([0x83 | {"0": 4, "1": 12, "2": 8}[self.tracking] | (32 if self.output else 0)])
        ch = int(payload[4])
        value = self.voltage[ch] if payload.startswith(("VSET", "VOUT")) else self.current[ch]
        if payload.startswith("IOUT") or (payload.startswith("VOUT") and not self.output):
            value = 0
        return f"{value:.3f}".encode()


def runtime_fixture(tmp_path):
    config = ConnectionConfig("mock://gpd", "gwinstek.gpd4303s", "mock", expected_serial="SN:TEST")
    runtime = ApplicationRuntime((config,), root=tmp_path, hardware=False)
    wire = SupplyFixture()

    def factory(selected):
        adapter = Gpd4303sAdapter(wire, gpd4303s(), selected)
        adapter.transport.interval = 0
        adapter.transport.transition_interval = 0
        return adapter

    runtime.sessions.factory = factory
    key = runtime.add(config)
    runtime.connect(key).result()
    return runtime, key, wire


def test_session_hardware_sequence_restore_reconnect_and_channel_snapshots(tmp_path):
    runtime, key, wire = runtime_fixture(tmp_path)
    report = {"operations": [], "stages": {}, "range_snapshots": [], "mode_snapshots": []}
    try:
        test = HardwareSession(runtime, key, report, tmp_path)
        original = test.snapshot()
        test.control_tests()
        test.restore(original)
        assert not report["restoration_differences"] and not wire.output
        assert report["stages"]["unloaded_output"] == "passed"
        snapshot = runtime.sessions.capability_snapshot(key)
        assert snapshot.channels[3].get("voltage").parameters["value"].maximum == 10
        assert len(wire.threads) == 1 and threading.get_ident() not in wire.threads
        runtime.sessions.disconnect(key)
        runtime.connect(key).result()
        assert test.snapshot()["status"]["output_enabled"] is False
    finally:
        runtime.close()


def test_restore_failure_preserves_evidence_and_does_not_replay(tmp_path):
    runtime, key, wire = runtime_fixture(tmp_path)
    report = {"operations": [], "stages": {}}
    try:
        test = HardwareSession(runtime, key, report, tmp_path)
        original = test.snapshot()
        test.apply("voltage", 1.0, 1)
        wire.fail_on = "VSET1:3.000"
        with pytest.raises(DeviceError):
            test.restore(original)
        assert wire.history.count("VSET1:3.000") == 1
        assert "error" in report["operations"][-1]
        assert (tmp_path / "results.json").exists()
    finally:
        runtime.close()


def test_local_stops_queries_and_remote_resumes_in_same_session(tmp_path):
    runtime, key, wire = runtime_fixture(tmp_path)
    try:
        test = HardwareSession(runtime, key, {"operations": []}, tmp_path)
        original = test.snapshot()
        test.apply("local")
        assert wire.history[-1] == "LOCAL"
        count = len(wire.history)
        snapshot = runtime.sessions.submit(key, "refresh_capabilities").result()
        assert snapshot.details["control_mode"] == "local"
        assert snapshot.channels[1].get("remote").writable
        assert not snapshot.channels[1].get("voltage").writable
        assert not snapshot.channels[1].get("all_outputs").writable
        with pytest.raises(DeviceError):
            runtime.sessions.submit(key, "read_settings", (("voltage",), 1)).result()
        with pytest.raises(DeviceError):
            test.apply("voltage", 1)
        assert len(wire.history) == count
        assert runtime.sessions.sessions[key].state == "connected"
        test.apply("remote")
        assert wire.history[count] == "REMOTE"
        assert test.snapshot() == original
        test.apply("local")
        runtime.sessions.disconnect(key)
        runtime.connect(key).result()
        assert test.snapshot() == original
        assert not any(command.startswith(("VSET1:", "OUT")) for command in wire.history)
    finally:
        runtime.close()


def test_transition_wait_is_model_owned_and_follows_write(monkeypatch):
    import time

    events = []

    class Wire:
        resource = "mock://gpd"

        def write(self, payload):
            events.append(payload)

    monkeypatch.setattr(time, "sleep", lambda seconds: events.append(seconds))
    paced = PacedTransport(Wire(), interval=0)
    paced.write("REMOTE")
    paced.write("LOCAL")
    paced.write("VSET1:1.000")
    assert events == ["REMOTE", 1.0, "LOCAL", 1.0, "VSET1:1.000"]


def test_unrestorable_original_blocks_test_until_target_explicitly_changed(tmp_path):
    runtime, key, wire = runtime_fixture(tmp_path)
    try:
        test = HardwareSession(runtime, key, {"operations": []}, tmp_path)
        original = test.snapshot()
        original["4.current"] = 1.2
        config = runtime.sessions.sessions[key].config
        with pytest.raises(DeviceError):
            validate_restoration_target(original, config)
        target = {**original, "4.current": 1.0}
        validate_restoration_target(target, config)
        assert original["4.current"] == 1.2
        assert all(command.endswith("?") or command == "REMOTE" for command in wire.history)
    finally:
        runtime.close()
