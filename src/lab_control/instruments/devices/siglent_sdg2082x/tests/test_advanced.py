import json
import struct
from types import SimpleNamespace

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import DeviceCommand, DeviceError, Quality, Unsupported, ValidationError
from lab_control.instruments.devices.siglent_sdg2082x.adapter import Sdg2082xAdapter, sdg2082x
from lab_control.instruments.devices.siglent_sdg2082x.hardware import validate_control
from lab_control.instruments.devices.siglent_sdg2082x.hardware.validate_control import Acceptance, Guard
from lab_control.instruments.devices.siglent_sdg2082x.protocol import (
    field,
    fields,
    parse_waveform,
    user_waveforms,
    waveform_bytes,
)
from lab_control.transport.base import MockTransport

COUP = b"COUP TRACE,OFF,FCOUP,OFF,PCOUP,OFF,ACOUP,OFF"
BASE = b"C1:BSWV WVTP,SINE,FRQ,10000HZ,AMP,0.2V,OFST,0V,PHSE,0"


def device(exchanges):
    transport = MockTransport(exchanges)
    transport.open()
    return Sdg2082xAdapter(transport, sdg2082x(), ConnectionConfig("mock", "siglent.sdg2082x")), transport


@pytest.mark.parametrize("header", ["C1:MDWV ", ""])
@pytest.mark.parametrize("prefix", ["AM,STATE,ON", "STATE,ON,AM"])
def test_nested_carrier_does_not_shadow_modulation_fields(header, prefix):
    raw = (header + prefix + ",SRC,INT,FRQ,100HZ,DEPTH,20,CARR,WVTP,SINE,FRQ,10000HZ,AMP,0.2V").encode()
    assert field(raw, "FRQ", "AM") == 100
    assert fields(raw)["CARR"]["FRQ"] == 10000
    with pytest.raises(Unsupported):
        field(raw, "FRQ", "FM")


def test_inactive_parameters_are_unavailable_and_never_fake_zero():
    d, t = device([("C1:MDWV?", b"C1:MDWV STATE,OFF")])
    reading = d.read_settings(("am_frequency",))[0]
    assert reading.value is None and reading.source == "unavailable" and reading.quality == Quality.STALE
    assert t.history == ["C1:MDWV?"]


def test_modulation_write_readback_uses_current_mode():
    response = b"C1:MDWV AM,STATE,ON,SRC,INT,FRQ,100HZ,DEPTH,10,CARR,WVTP,SINE,FRQ,10000HZ"
    d, t = device(
        [
            ("COUP?", COUP),
            ("C1:BSWV?", BASE),
            ("C1:MDWV?", response),
            ("C1:MDWV AM,FRQ,1000", None),
            ("C1:MDWV?", response.replace(b"FRQ,100HZ", b"FRQ,1000HZ")),
        ]
    )
    result = d.apply(DeviceCommand("am_frequency", {"value": 1000}, confirmed=True))
    assert result.outcome == "success" and result.readback == 1000
    assert not any("C2:" in str(item) for item in t.history)


@pytest.mark.parametrize(
    "operation,response,value",
    [
        ("fm_deviation", b"FM,STATE,ON,SRC,INT,DEVI,1,CARR,WVTP,RAMP,FRQ,999900HZ", 101),
        ("am_frequency", b"FM,STATE,ON,SRC,INT,FRQ,100", 1000),
        ("am_frequency", b"AM,STATE,ON,SRC,EXT,FRQ,100", 1000),
        ("sweep_start", b"STATE,ON,START,100HZ,STOP,200HZ,CARR,WVTP,SINE,FRQ,1000HZ", 201),
        ("sweep_stop", b"STATE,ON,START,100HZ,STOP,200HZ,CARR,WVTP,RAMP,FRQ,1000HZ", 1e6 + 1),
        ("burst_cycles", b"STATE,ON,GATE_NCYC,GATE,CARR,WVTP,SINE,FRQ,1000HZ", 5),
        ("burst_period", b"STATE,ON,TRSR,EXT,CARR,WVTP,SINE,FRQ,1000HZ", 0.01),
        ("burst_period", b"STATE,ON,TRSR,MAN,CARR,WVTP,SINE,FRQ,1000HZ", 0.01),
    ],
)
def test_invalid_advanced_parameters_never_write(operation, response, value):
    group = "SWWV" if operation.startswith("sweep") else "BTWV" if operation.startswith("burst") else "MDWV"
    d, t = device([("COUP?", COUP), ("C1:BSWV?", BASE), (f"C1:{group}?", response)])
    with pytest.raises(ValidationError):
        d.apply(DeviceCommand(operation, {"value": value}, confirmed=True))
    assert all("?" in item for item in t.history)


def test_mode_enable_requires_other_modes_off_without_implicit_switching():
    d, t = device(
        [("COUP?", COUP), ("C1:BSWV?", BASE), ("C1:SWWV?", b"STATE,OFF"), ("C1:MDWV?", b"STATE,ON,AM")]
    )
    with pytest.raises(ValidationError, match="other advanced modes"):
        d.apply(DeviceCommand("sweep", {"value": True}, confirmed=True))
    assert all("?" in item for item in t.history)


@pytest.mark.parametrize(
    "operation,value,basic",
    [
        ("pulse_width", 1e-3, b"WVTP,PULSE,FRQ,1000HZ,WIDTH,0.0005S,RISE,8.4E-9S,FALL,8.4E-9S"),
        ("pulse_duty", 0.001, b"WVTP,PULSE,FRQ,1000HZ,RISE,8.4E-9S,FALL,8.4E-9S"),
        ("pulse_width", 0.00025, BASE),
        ("ramp_symmetry", 25, BASE),
    ],
)
def test_pulse_and_ramp_constraints_never_write(operation, value, basic):
    d, t = device([("COUP?", COUP), ("C1:BSWV?", basic)])
    with pytest.raises(ValidationError):
        d.apply(DeviceCommand(operation, {"value": value}, confirmed=True))
    assert all("?" in item for item in t.history)


def test_waveform_framing_preserves_newline_bytes_and_checks_length():
    data = struct.pack("<8h", 10, 13, -32767, 32767, 0, 2573, 3338, -10)
    raw = b"WVDT WVNM,test,LENGTH,16B,FREQ,1000HZ,WAVEDATA," + data + b"\n"
    assert parse_waveform(raw, "test")[1] == data
    assert waveform_bytes([-1, 1, 0, 0, 0, 0, 0, 0])[:4] == struct.pack("<2h", -32767, 32767)
    for invalid in (raw[:-5], raw + b"garbage", raw.replace(b"16B", b"17B")):
        with pytest.raises(DeviceError):
            parse_waveform(invalid, "test")
    with pytest.raises(DeviceError):
        parse_waveform(raw, "different")
    assert user_waveforms(b"STL WVNM") == ()


@pytest.mark.parametrize("readback", ["match", "mismatch", "timeout"])
def test_upload_verifies_every_byte_and_never_replays_on_failure(readback):
    data = waveform_bytes([0.5] * 8)
    payload = b"C1:WVDT WVNM,test,WAVEDATA," + data
    response = b"WVDT WVNM,test,LENGTH,16B,WAVEDATA," + (data if readback == "match" else b"\0" * 16)
    d, t = device(
        [
            ("COUP?", COUP),
            ("C1:BSWV?", BASE),
            ("C1:OUTP?", b"OFF,LOAD,HZ"),
            *[(f"C1:{g}?", b"STATE,OFF") for g in ("MDWV", "SWWV", "BTWV")],
            ("STL? USER", b"STL WVNM"),
            (payload, None),
            ("WVDT? USER,test", DeviceError("timeout", "timeout") if readback == "timeout" else response),
        ]
    )
    result = d.apply(DeviceCommand("upload_waveform", {"name": "test", "samples": [0.5] * 8}, confirmed=True))
    assert result.outcome == {"match": "success", "mismatch": "rejected", "timeout": "partial"}[readback]
    assert t.history.count(payload) == 1


@pytest.mark.parametrize(
    "payload",
    [
        "C1:OUTP ON",
        "C1:SYNC ON",
        "C1:SWWV MTRIG",
        "C1:BTWV TRMD,RISE",
        "*RST",
        "*CLS",
        "DEL_STORE_FILE old.bin",
        b"C1:WVDT WVNM,x,WAVEDATA,\x00",
    ],
)
def test_acceptance_guard_blocks_forbidden_actions_even_if_allowlisted(tmp_path, payload):
    transport = MockTransport([])
    guard = Guard(transport, tmp_path)
    guard.settings = True
    guard.allowed_settings.add(payload)
    with pytest.raises(DeviceError):
        guard.write(payload)
    assert transport.history == []


def test_failed_restoration_stops_further_writes(tmp_path):
    runner = Acceptance(ConnectionConfig("mock", "siglent.sdg2082x", expected_serial="TEST"), tmp_path)
    runner.backups = {"1.base": {"WVTP": "SINE"}, "2.base": {"WVTP": "SINE"}}
    calls = []

    def fail(channel):
        calls.append(channel)
        raise DeviceError("timeout", "timeout")

    runner.quiet = fail
    with pytest.raises(DeviceError):
        runner.restore()
    assert calls == [1]


def test_failure_to_read_or_close_still_archives_final_state_as_unknown(tmp_path):
    folder = tmp_path / "asset" / "batch"
    folder.mkdir(parents=True)
    runner = Acceptance(ConnectionConfig("mock", "siglent.sdg2082x", expected_serial="TEST"), folder)
    closed = []

    def close():
        closed.append(True)
        raise DeviceError("close failed", "disconnected")

    def connect():
        runner.app = SimpleNamespace(close=close)
        runner.adapter = object()
        raise DeviceError("connection failed", "disconnected")

    def snapshot():
        raise DeviceError("snapshot timeout", "timeout")

    runner.connect, runner.snapshot = connect, snapshot
    runner.run(preflight_only=True)
    saved = json.loads((folder / "results.json").read_text(encoding="utf-8"))
    assert saved["final_snapshot_error"] == "snapshot timeout"
    assert saved["close_error"] == "close failed" and saved["session_released"] is False
    assert "final_observed_snapshot" not in saved
    assert closed == [True]
    assert (folder / "record.md").exists() and (tmp_path / "index.md").exists()


@pytest.mark.parametrize("operation,value", [("frequency", 2000), ("waveform", "RAMP"), ("phase", 90)])
def test_basic_changes_cannot_bypass_active_modulation_limits(operation, value):
    exchanges = [("COUP?", COUP), ("C1:BSWV?", BASE)]
    if operation != "phase":
        exchanges.append(("C1:OUTP?", b"OFF,LOAD,HZ"))
    exchanges.append(("C1:MDWV?", b"FM,STATE,ON"))
    d, t = device(exchanges)
    with pytest.raises(ValidationError, match="Disable advanced"):
        d.apply(DeviceCommand(operation, {"value": value}, confirmed=True))
    assert all("?" in item for item in t.history)


@pytest.mark.parametrize("operation,value", [("fm_deviation", 200), ("sweep_stop", 21e6)])
def test_advanced_frequencies_cannot_bypass_load_amplitude_limits(operation, value):
    group = "MDWV" if operation == "fm_deviation" else "SWWV"
    current = b"FM,STATE,ON,SRC,INT,START,1HZ,STOP,20E6HZ,CARR,WVTP,SINE,FRQ,20E6HZ,AMP,15V,OFST,0V"
    d, t = device(
        [("COUP?", COUP), ("C1:BSWV?", BASE), (f"C1:{group}?", current), ("C1:OUTP?", b"OFF,LOAD,HZ")]
    )
    with pytest.raises(ValidationError, match="amplitude/load"):
        d.apply(DeviceCommand(operation, {"value": value}, confirmed=True))
    assert all("?" in item for item in t.history)


@pytest.mark.parametrize("actual,outcome", [(1.001, "success"), (1.002, "rejected")])
def test_documented_duty_quantization_keeps_actual_readback(actual, outcome):
    basic = b"WVTP,PULSE,FRQ,1000HZ,WIDTH,0.0005S,RISE,8.4E-9S,FALL,8.4E-9S"
    d, _ = device(
        [("COUP?", COUP), ("C1:BSWV?", basic)]
        + [(f"C1:{group}?", b"STATE,OFF") for group in ("MDWV", "SWWV", "BTWV")]
        + [("C1:BSWV DUTY,1.0014", None), ("C1:BSWV?", f"DUTY,{actual}".encode())]
    )
    result = d.apply(DeviceCommand("pulse_duty", {"value": 1.0014}, confirmed=True))
    assert result.outcome == outcome and result.readback == actual
    assert result.requested["value"] == 1.0014


def test_stability_query_spacing_survives_variable_worker_latency(tmp_path, monkeypatch):
    runner = Acceptance(ConnectionConfig("mock", "siglent.sdg2082x", expected_serial="TEST"), tmp_path)
    runner.initial = {"C1:BSWV?": "one", "C2:BSWV?": "two"}
    now = [0.0]
    starts, disconnected = [], []

    def sleep(seconds):
        now[0] += seconds

    def query(command):
        if command == "C1:BSWV?":
            # Alternating queue latency previously made actual query starts <1s apart.
            now[0] += 0.025 if len(starts) % 2 == 0 else 0
            starts.append(now[0])
        now[0] += 0.01
        return runner.initial[command]

    monkeypatch.setattr(validate_control.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(validate_control.time, "sleep", sleep)
    runner.query, runner.outputs_off = query, lambda: None
    runner.snapshot = lambda: dict(runner.initial)
    runner.app = SimpleNamespace(
        sessions=SimpleNamespace(disconnect=lambda value: disconnected.append(value)),
        connect=lambda value, *, configure_acquisition: SimpleNamespace(
            result=lambda timeout: (
                {"serial": "TEST"}
                if not configure_acquisition
                else pytest.fail("Read-only reconnect must skip setup")
            )
        ),
    )
    runner.stability()
    assert len(starts) == 60 and min(b - a for a, b in zip(starts, starts[1:])) >= 1
    assert len(disconnected) == 3 and runner.report["stages"]["reconnection"] == "passed"


@pytest.mark.parametrize(
    "group,values,mode",
    [
        ("MDWV", {"SRC": "EXT", "DEPTH": 20, "CARR": {}}, "AM"),
        ("BTWV", {"GATE_NCYC": "GATE", "TRSR": "EXT", "CARR": {}}, ""),
        ("SWWV", {"START": 1000, "STOP": 2000, "CARR": {}}, ""),
    ],
)
def test_missing_hidden_backup_values_block_parameter_matrix(group, values, mode):
    with pytest.raises(RuntimeError, match="backup"):
        validate_control.require_complete_backup(group, values, mode)
