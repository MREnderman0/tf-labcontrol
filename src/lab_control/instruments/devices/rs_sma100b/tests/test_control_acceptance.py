from dataclasses import replace

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import (
    DeviceCommand,
    DeviceError,
    DeviceIdentity,
    Unsupported,
    ValidationError,
)
from lab_control.instruments.devices.rs_sma100b.hardware.validate_control import (
    Acceptance,
    Guard,
    differences,
)
from lab_control.instruments.registry import build_scpi_adapter
from lab_control.transport.base import MockTransport, ieee_block


def device(exchanges=(), **kwargs):
    transport = MockTransport(list(exchanges))
    transport.open()
    config = ConnectionConfig("mock://test", "rs.sma100b", **kwargs)
    return build_scpi_adapter(config, transport), transport


@pytest.mark.parametrize("mode", ("am", "fm", "pm"))
@pytest.mark.parametrize("value,wire", (("INT", "LF1"), ("EXT", "EXT1")))
def test_modulation_source_aliases_keep_public_api(mode, value, wire):
    adapter, transport = device(
        [
            (f"SOUR:{mode.upper()}1:SOUR {wire}", b""),
            (f"SOUR:{mode.upper()}1:SOUR?", wire.encode()),
            ("SYST:ERR?", b"0,No error"),
        ],
        options=("SMAB-K720",),
    )
    result = adapter.apply(DeviceCommand(mode + "_source", {"value": value}))
    assert result.outcome == "success" and result.readback == value
    assert not transport.exchanges


def test_installed_options_gate_modulation_and_pulse_without_configuration_spoofing():
    adapter, transport = device(options=("SMAB-K720", "SMAB-K22"))
    adapter.identity = DeviceIdentity("Rohde&Schwarz", "SMA100B", "TEST")
    with pytest.raises(Unsupported):
        adapter.capabilities().get("am_enabled")
    adapter.detected_options = ("SMAB-K720",)
    assert adapter.capabilities().get("am_enabled").available
    assert not adapter.capabilities().get("am_shape").writable
    with pytest.raises(Unsupported):
        adapter.capabilities().get("pulse_enabled")
    assert not transport.history


def test_list_arrays_follow_instrument_range_and_experiment_limits_and_stale_state():
    adapter, transport = device(limits={"power": -10, "frequency": 6e9})
    adapter.dynamic_parameters = {
        "frequency": replace(
            adapter.profile.commands["frequency"].capability.parameters["value"],
            maximum=12.75e9,
            range_source="instrument",
        ),
        "power": replace(
            adapter.profile.commands["power"].capability.parameters["value"],
            minimum=-145,
            maximum=40,
            range_source="instrument",
        ),
    }
    cap = adapter.capabilities().get("upload_list")
    good = {"name": "test", "frequencies": [6e9], "powers": [-10]}
    assert cap.validate(good)
    for values in ({**good, "frequencies": [7e9]}, {**good, "powers": [0]}, {**good, "frequencies": [8e3]}):
        with pytest.raises(ValidationError):
            cap.validate(values)
    adapter.capability_error = "range query failed"
    with pytest.raises(Unsupported):
        adapter.capabilities().get("upload_list")
    assert not transport.history


def test_existing_list_cannot_be_overwritten():
    adapter, transport = device([("SOUR:FREQ:MODE?", b"CW"), ("SOUR:LIST:CAT?", b"original,test")])
    with pytest.raises(ValidationError, match="existing lists"):
        adapter.apply(
            DeviceCommand(
                "upload_list", {"name": "test", "frequencies": [1e6], "powers": [-40]}, confirmed=True
            )
        )
    assert all(command.endswith("?") for command in transport.history)


def test_malformed_list_counts_are_rejected_without_retry():
    adapter, transport = device(
        [
            ("FORM?", b"ASC"),
            ("SOUR:LIST:SEL?", b"test"),
            ("SOUR:LIST:FREQ:POIN?", b"3"),
            ("SOUR:LIST:FREQ?", b"1,2"),
        ]
    )
    with pytest.raises(ValidationError, match="complete response"):
        adapter.read_list()
    assert transport.history.count("SOUR:LIST:FREQ?") == 1


def test_full_list_file_backup_preserves_exact_binary():
    raw = b"header\x00\xff\noriginal dwell and frequency data"
    adapter, _ = device(
        [
            ("SOUR:LIST:SEL?", b"/var/user/original.lsw"),
            ("MMEM:DATA? '/var/user/original.lsw'", ieee_block(raw)),
        ]
    )
    assert adapter.backup_list_file("/var/user/original.lsw") == raw


def test_list_cleanup_refuses_unowned_files_without_io():
    adapter, transport = device()
    with pytest.raises(ValidationError):
        adapter.delete_created_list("original", {"LCtest"})
    assert not transport.history


def test_modulation_coupling_and_active_auxiliary_output_block_writes():
    adapter, transport = device([("SOUR:AM:TYPE?", b"EXP")], options=("SMAB-K720",))
    with pytest.raises(ValidationError, match="linear"):
        adapter.apply(DeviceCommand("am_depth", {"value": 10}))
    assert transport.history == ["SOUR:AM:TYPE?"]
    adapter, transport = device(
        [("SOUR:AM1:SOUR?", b"LF1"), ("SOUR:LFO:FREQ:MODE?", b"CW"), ("SOUR:LFO1:STAT?", b"1")],
        options=("SMAB-K720",),
    )
    with pytest.raises(ValidationError, match="auxiliary"):
        adapter.apply(DeviceCommand("am_frequency", {"value": 100}))
    assert all(command.endswith("?") for command in transport.history)


def test_pm_radians_and_current_mode_limit():
    adapter, transport = device(
        [
            ("SOUR:PM:DEV:MODE?", b"UNC"),
            ("SOUR:PM1:DEV? MAX", b"1"),
            ("SOUR:PM1:DEV 0.2", b""),
            ("SOUR:PM1:DEV?", b"0.2"),
            ("SYST:ERR?", b"0,No error"),
        ],
        options=("SMAB-K720",),
    )
    assert adapter.apply(DeviceCommand("pm_deviation", {"value": 0.2})).outcome == "success"
    assert not transport.exchanges


def test_fm_deviation_respects_lower_carrier_band_edge_before_write():
    adapter, transport = device(
        [("SOUR:FM:DEV:MODE?", b"UNC"), ("SOUR:FM1:DEV? MAX", b"1000000"), ("SOUR:FREQ?", b"9000")],
        options=("SMAB-K720",),
    )
    with pytest.raises(ValidationError, match="bandwidth margins"):
        adapter.apply(DeviceCommand("fm_deviation", {"value": 2000}))
    assert all("?" in command for command in transport.history)


@pytest.mark.parametrize(
    "command",
    [
        "OUTP ON",
        "*RST",
        "*CLS",
        "*TRG",
        "SOUR:LIST:TRIG:EXEC",
        "SOUR:LIST:DEL 'original'",
        "SOUR:POW 21",
        "SOUR:POW:LIM 40",
        "SOUR:LFO1:STAT ON",
    ],
)
def test_acceptance_guard_rejects_rf_on_triggers_and_unowned_writes(tmp_path, command):
    inner = MockTransport([])
    guard = Guard(inner, tmp_path)
    guard.settings = True
    guard.allowed_settings = {"OUTP", "SOUR:LIST:DEL", "SOUR:POW", "SOUR:POW:LIM", "SOUR:LFO1:STAT"}
    with pytest.raises(RuntimeError):
        guard.write(command)
    assert not inner.history


def test_setting_timeout_is_not_replayed():
    adapter, transport = device(
        [("SOUR:FREQ 100000000", b""), ("SOUR:FREQ?", DeviceError("timeout", "timeout"))]
    )
    result = adapter.apply(DeviceCommand("frequency", {"value": 1e8}))
    assert result.outcome == "partial"
    assert transport.history.count("SOUR:FREQ 100000000") == 1


def test_known_firmware_list_mode_conflict_is_blocked_without_enabling_rf():
    adapter, transport = device([("OUTP?", b"0")])
    adapter.identity = DeviceIdentity("Rohde&Schwarz", "SMA100B", "TEST", "4.60.112.29 SP6")
    with pytest.raises(Unsupported, match="RF OFF"):
        adapter.preflight(DeviceCommand("frequency_mode", {"value": "LIST"}), {"value": "LIST"})
    assert transport.history == ["OUTP?"]


def test_restoration_failure_is_saved_and_does_not_continue(tmp_path, monkeypatch):
    runner = Acceptance(ConnectionConfig("mock://test", "rs.sma100b"), tmp_path)
    runner.guards.append(Guard(MockTransport([]), tmp_path))
    monkeypatch.setattr(runner, "outputs_off", lambda: (_ for _ in ()).throw(RuntimeError("link lost")))
    with pytest.raises(RuntimeError, match="link lost"):
        runner.restore()
    assert runner.report["stages"]["restoration"] == "failed"
    assert runner.report["restore_steps"] == [{"status": "failed", "error": "link lost"}]
    assert (tmp_path / "results.json").exists()
    assert differences({"RF": "0"}, {"RF": "1"})


def test_readonly_verification_can_confirm_restoration_without_replaying_error(tmp_path, monkeypatch):
    runner = Acceptance(ConnectionConfig("mock://test", "rs.sma100b"), tmp_path)
    inner = MockTransport([])
    runner.guards.append(Guard(inner, tmp_path))
    runner.initial = {"OUTP": "0", "SOUR:POW": "21"}
    calls = []

    def outputs_off():
        calls.append("check")
        if len(calls) == 1:
            raise RuntimeError("Instrument reported original level limit conflict")
        return {"OUTP": "0"}

    monkeypatch.setattr(runner, "outputs_off", outputs_off)
    monkeypatch.setattr(runner, "snapshot", lambda **kwargs: runner.initial.copy())
    monkeypatch.setattr(runner, "verify_original_list", lambda: None)
    runner.restore()
    assert runner.report["stages"]["restoration"] == "passed"
    assert "conflict" in runner.report["restoration_warning"]
    assert not runner.report["restore_differences"] and not inner.history


def test_active_other_modulation_blocks_shared_generator_write(tmp_path):
    adapter, transport = device(
        [
            ("SOUR:FM1:SOUR?", b"LF1"),
            ("SOUR:LFO:FREQ:MODE?", b"CW"),
            ("SOUR:LFO1:STAT?", b"0"),
            ("SOUR:LFO2:STAT?", b"0"),
            ("SOUR:AM1:STAT?", b"1"),
        ],
        options=("SMAB-K720",),
    )
    with pytest.raises(ValidationError, match="Another active"):
        adapter.apply(DeviceCommand("fm_frequency", {"value": 1000}))
    assert all(command.endswith("?") for command in transport.history)


def test_restore_power_exception_only_accepts_exact_backed_up_value(tmp_path):
    inner = MockTransport([("SOUR:POW 21", b"")])
    inner.open()
    guard = Guard(inner, tmp_path)
    guard.settings = guard.restore = True
    guard.allowed_settings = {"SOUR:POW"}
    guard.restore_values = {"SOUR:POW": "21"}
    with pytest.raises(RuntimeError):
        guard.write("SOUR:POW 22")
    guard.write("SOUR:POW 21")
    assert inner.history == ["SOUR:POW 21"]
