from types import SimpleNamespace

import pytest

from lab_control.domain.instruments import DeviceError

from ..hardware.thermistor_bank import ThermistorBankTest, parse_groups
from .test_hardware_entry import done


def bank(tmp_path, nominals=None):
    return ThermistorBankTest(
        None, "device", tmp_path, {"operations": [], "samples": []}, nominals or {401: 5000, 417: 10000}
    )


def test_groups_are_explicit_nonoverlapping_physical_channels():
    values = parse_groups(["401:416=5000", "417:419=10000"])
    assert values == {**dict.fromkeys(range(401, 417), 5000), **dict.fromkeys(range(417, 420), 10000)}
    for groups in (
        [],
        ["1:16=5000"],
        ["401:417=5000", "417=10000"],
        ["401=9999"],
        ["401:501=5000"],
        ["433=5000"],
        ["401=5000=10000"],
    ):
        with pytest.raises((ValueError, DeviceError)):
            parse_groups(groups)


def test_temperature_settings_preserve_group_nominals_and_backup_before_write(tmp_path):
    test = bank(tmp_path)
    history = []

    def q(query):
        history.append(("query", query))
        if query.startswith("TEMP:TRAN:TYPE?"):
            return "TC"
        if query.startswith("UNIT:TEMP?"):
            return "K"
        return "3000"

    test.q = q
    test.apply = lambda *args: history.append(("apply", args))
    test.configure_temperature(10000)
    assert ("apply", ("thermistor_type", 5000, 401)) in history
    assert ("apply", ("thermistor_type", 10000, 417)) in history
    assert history.index(("query", "TEMP:TRAN:THER:TYPE? (@401)")) < history.index(
        ("apply", ("thermistor_type", 5000, 401))
    )
    assert test.original_temperature[401] == dict(sensor="TC", unit="K", thermistor_type=3000)
    assert history[-1] == ("apply", ("scan_list", "(@401,417)"))


def test_restore_continues_after_failure_and_handles_partial_group_configuration(tmp_path):
    test = bank(tmp_path)
    test.initial = dict(function_401="VOLT", function_417="TEMP", scan_list="(@)")
    test.touched = {401, 417}
    test.original_temperature = {401: dict(sensor="TC", unit="K", thermistor_type=3000)}
    values = {
        "FUNC? (@401)": "RES",
        "FUNC? (@417)": "RES",
        "ROUT:SCAN?": "(@401,417)",
        "TEMP:TRAN:TYPE? (@401)": "THER",
        "TEMP:TRAN:THER:TYPE? (@401)": "5000",
        "UNIT:TEMP? (@401)": "C",
        "SYST:ERR?": "+0,No error",
    }
    history = []
    queries = dict(function="FUNC", temperature_sensor="TEMP:TRAN:TYPE", temperature_unit="UNIT:TEMP")

    def apply(operation, expected, ch=1):
        history.append((operation, expected, ch))
        if operation == "thermistor_type":
            raise DeviceError("uncertain restoration timeout", "timeout")
        query = "ROUT:SCAN?" if operation == "scan_list" else f"{queries[operation]}? (@{ch})"
        values[query] = expected

    test.q = lambda query: values[query]
    test.apply = apply
    test.snapshot = lambda: dict(test.initial)
    result = test.restore()
    assert result["state"] == "failed" and "timeout" in str(result["errors"])
    assert history[0] == ("function", "TEMP", 401)
    assert ("function", "TEMP", 417) in history
    assert ("temperature_sensor", "TC", 401) in history
    assert history.count(("thermistor_type", 3000, 401)) == 1
    assert history[-1] == ("scan_list", "(@)", 1)


def test_unselected_input_write_is_refused_before_session_io(tmp_path):
    test = bank(tmp_path)
    with pytest.raises(ValueError):
        test.apply("function", "RES", 420)
    assert not test.modified and not test.touched


def test_capture_rejects_missing_channel_and_never_saves_partial_bank(tmp_path):
    test = bank(tmp_path)
    partial = SimpleNamespace(measurements=[SimpleNamespace(channel=401, values=(5000,), quality="good")])
    test.app = SimpleNamespace(sessions=SimpleNamespace(submit=lambda *args: done(partial)))
    with pytest.raises(DeviceError, match="Invalid input frame"):
        test.capture("resistance", 1)
    assert not list(tmp_path.iterdir())


def test_busy_scan_preflight_preserves_settings(tmp_path):
    test = bank(tmp_path)
    initial = {
        "function_401": "VOLT",
        "function_417": "VOLT",
        "scan_list": "(@420)",
        "monitor_state": "0",
        "dmm_state": "1",
        "trigger_source": "IMM",
        "trigger_count": "1",
        **dict.fromkeys((f"format_{key}" for key in ("UNIT", "TIME", "CHAN", "ALAR")), "0"),
        "relays_400": ",".join(["0"] * 32),
    }
    test.snapshot = lambda: initial
    with pytest.raises(DeviceError, match="no test writes"):
        test.prepare()
    assert not test.modified and not test.report["operations"]
