from __future__ import annotations

import numpy as np
import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError, DeviceIdentity
from lab_control.instruments.devices.ceyear_6362d import DEFINITION
from lab_control.instruments.devices.ceyear_6362d import protocol as p
from lab_control.instruments.devices.ceyear_6362d.adapter import Ceyear6362DAdapter
from lab_control.instruments.devices.ceyear_6362d.hardware.state import (
    qualify_binary,
    restore,
    set_endpoints,
    snapshot,
)
from lab_control.instruments.devices.ceyear_6362d.hardware.validate_control import AuditTransport, read_trace
from lab_control.transport.base import MockTransport, ieee_block


class Instrument:
    resource = "mock://6362d"

    def __init__(self):
        self.history = []
        self.values = {
            "*IDN?": b"Ceyear Technologies,6362D,TEST-6362D,2.1.0",
            ":UNIT:X?": b"0",
            ":DISPLAY:TRACE:Y1:UNIT?": b"0",
            ":SENSE:WAVELENGTH:START?": b"1.54e-6",
            ":SENSE:WAVELENGTH:STOP?": b"1.56e-6",
            ":SENSE:WAVELENGTH:CENTER?": b"1.55e-6",
            ":SENSE:WAVELENGTH:SPAN?": b"2e-8",
            ":SENSE:BANDWIDTH:RESOLUTION?": b"1e-10",
            ":SENSE:SENSE?": b"6",
            ":SENSE:SWEEP:POINTS?": b"1001",
            ":SENSE:SWEEP:POINTS:AUTO?": b"0",
            ":INITIATE:SMODE?": b"1",
            ":TRACE:ATTRIBUTE:TRA?": b"0",
            ":TRACE:ACTIVE?": b"TRA",
            ":FORMAT:DATA?": b"ASCII",
            ":SENSE:CORRECTION:RVELOCITY:MEDIUM?": b"0",
            ":SENSE:CORRECTION:WAVELENGTH:SHIFT?": b"0",
            ":SENSE:CORRECTION:LEVEL:SHIFT?": b"0",
            ":SENSE:WAVELENGTH:SRANGE?": b"0",
            ":TRACE:DATA:SNUMBER? TRA": b"3",
            ":TRACE:DATA:X? TRA": b"1.54e-6,1.55e-6,1.56e-6",
            ":TRACE:DATA:Y? TRA": b"-62.5,-12.0,-60.5",
            ":STATUS:OPERATION:CONDITION?": b"1",
        }

    def query(self, command):
        self.history.append(command)
        value = self.values[command]
        if isinstance(value, Exception):
            raise value
        return value() if callable(value) else value

    def write(self, command):
        self.history.append(command)
        if command.startswith(":FORMAT:DATA "):
            self.values[":FORMAT:DATA?"] = command.split(" ", 1)[1].encode()

    def close(self):
        pass


def adapter(transport=None):
    tr = transport or Instrument()
    item = Ceyear6362DAdapter(
        tr, DEFINITION.profile, ConnectionConfig(tr.resource, "ceyear.6362d", timeout=0.2)
    )
    item.identify()
    return item


@pytest.mark.parametrize("fmt,size", [("REAL,32", 4), ("REAL,64", 8)])
def test_binary_backup_uses_block_framing_without_setting_writes(tmp_path, fmt, size):
    payload = ieee_block(b"binary\nwith-embedded-newline")

    class BinaryWire(Instrument):
        def query(self, command):
            raise AssertionError("Binary backups must not use line reads")

        def query_binary(self, command, maximum):
            assert command == ":TRACE:DATA:Y? TRB"
            assert maximum == 50001 * size
            return payload

    wire = AuditTransport(BinaryWire(), tmp_path, DEFINITION.profile)
    assert read_trace(wire, "TRB", "Y", fmt) == payload
    assert (tmp_path / "backup-wire-0001.bin").read_bytes() == payload
    assert not wire.writes_allowed


@pytest.mark.parametrize("identity", [b"Ceyear,6362C,TEST,2.1.0", b"Another,6362D,TEST,2.1.0"])
def test_identity_conflicts_before_setup(identity):
    tr = MockTransport([("*IDN?", identity)])
    tr.open()
    with pytest.raises(DeviceError) as error:
        adapter(tr)
    assert error.value.code == "identity_conflict"
    assert tr.history == ["*IDN?"]


@pytest.mark.parametrize("version,serial", [("1.1.0", "TEST"), ("unknown", "TEST"), ("2.1.0", "N/A")])
def test_firmware_or_untrusted_identity_forbids_control(version, serial):
    item = adapter()
    item.identity = DeviceIdentity("Ceyear", "6362D", serial, version)
    with pytest.raises(DeviceError):
        item.apply(DeviceCommand("sensitivity", {"value": "MID"}))
    assert item.transport.history == ["*IDN?"]


@pytest.mark.parametrize(
    "name,value",
    [
        ("rbw", 0.15),
        ("sweep_points", 100),
        ("sweep_points", 1.5),
        ("start_wavelength", 599),
        ("wavelength_span", 0),
    ],
)
def test_invalid_ranges_do_no_io(name, value):
    item = adapter()
    with pytest.raises(DeviceError):
        item.apply(DeviceCommand(name, {"value": value}))
    assert item.transport.history == ["*IDN?"]


@pytest.mark.parametrize(
    "op,value",
    [
        ("start_wavelength", 1600),
        ("stop_wavelength", 1500),
        ("center_wavelength", 600),
        ("wavelength_span", 1000),
    ],
)
def test_invalid_wavelength_combinations_never_write(op, value):
    item = adapter()
    with pytest.raises(DeviceError):
        item.apply(DeviceCommand(op, {"value": value}))
    assert all("?" in command for command in item.transport.history)


def test_auto_points_and_auto_scan_forbid_unrestorable_writes():
    item = adapter()
    item.transport.values[":SENSE:SWEEP:POINTS:AUTO?"] = b"1"
    with pytest.raises(DeviceError):
        item.apply(DeviceCommand("sweep_points", {"value": 1001}))
    item.transport.values[":INITIATE:SMODE?"] = b"3"
    with pytest.raises(DeviceError):
        item.apply(DeviceCommand("stop", confirmed=True))
    assert all("?" in command for command in item.transport.history)


def test_nm_write_si_readback_and_mismatch_not_replayed():
    item = adapter()
    good = item.apply(DeviceCommand("rbw", {"value": 0.1}))
    assert good.outcome == "success"
    assert ":SENSE:BANDWIDTH:RESOLUTION 0.1NM" in item.transport.history
    bad = item.apply(DeviceCommand("rbw", {"value": 0.2}))
    assert bad.outcome == "rejected"
    assert item.transport.history.count(":SENSE:BANDWIDTH:RESOLUTION 0.2NM") == 1


def test_scan_completion_uses_vendor_status_with_bounded_timeout():
    item = adapter()
    item.transport.values[":STATUS:OPERATION:CONDITION?"] = b"0"
    result = item.apply(DeviceCommand("single", confirmed=True))
    assert result.outcome == "partial" and "timeout" in result.error
    assert item.transport.history.count(":INITIATE") == 1
    assert "*OPC?" not in item.transport.history


def test_initiated_acquisition_sends_one_single_command_per_request():
    item = adapter()
    batch = item.acquire(AcquisitionRequest("spectrum", initiate=True, confirmed=True))
    assert batch.measurements[0].axis_unit == "nm"
    assert item.transport.history.count(":INITIATE:SMODE SINGLE") == 1
    assert item.transport.history.count(":INITIATE") == 1


def test_initiated_acquisition_requires_confirmation_before_any_io():
    item = adapter()
    with pytest.raises(DeviceError, match="confirmation"):
        item.acquire(AcquisitionRequest("spectrum", initiate=True))
    assert item.transport.history == ["*IDN?"]


def test_initiated_acquisition_timeout_is_not_retried():
    item = adapter()
    item.transport.values[":STATUS:OPERATION:CONDITION?"] = b"0"
    with pytest.raises(DeviceError) as error:
        item.acquire(AcquisitionRequest("spectrum", initiate=True, confirmed=True))
    assert error.value.code == "acquisition_failed"
    assert item.transport.history.count(":INITIATE") == 1


def test_repeat_status_is_not_mislabeled_as_idle():
    item = adapter()
    item.transport.values[":INITIATE:SMODE?"] = b"2"
    result = item.apply(DeviceCommand("stop", confirmed=True))
    assert result.readback["running_state"] == "unknown"


def test_identity_is_readonly_and_unqualified_setup_is_explicitly_blocked():
    item = adapter()
    assert item.transport.history == ["*IDN?"]
    with pytest.raises(DeviceError, match="archived qualification"):
        item.prepare_acquisition()
    assert item.acquisition_preparation == {"status": "blocked", "writes": []}
    assert item.transport.history == ["*IDN?"]


def test_observed_cy_prefix_is_an_exact_model_alias():
    wire = Instrument()
    wire.values["*IDN?"] = b"Ceyear Technologies,CY6362D,TEST-6362D,2.2.1"
    assert adapter(wire).identity.model == "CY6362D"


def test_real32_is_not_enabled_by_real64_qualification():
    item = adapter()
    item.identity = DeviceIdentity("Ceyear", "CY6362D", "TEST-6362D", "2.2.1")
    item.transport.values[":FORMAT:DATA?"] = b"REAL,32"
    with pytest.raises(DeviceError, match="REAL32 acquisition"):
        item.acquire(AcquisitionRequest("spectrum"))
    assert all("?" in command for command in item.transport.history)


def test_ascii_acquisition_has_real_units_full_count_and_no_writes():
    item = adapter()
    batch = item.acquire(AcquisitionRequest("spectrum", points=1001))
    trace = batch.measurements[0]
    assert (trace.unit, trace.axis_unit) == ("dBm", "nm")
    np.testing.assert_allclose(trace.axis, [1540, 1550, 1560])
    assert len(trace.values) == 3 and trace.metadata["requested_points"] == 1001
    assert batch.validation == "full" and all("?" in c for c in item.transport.history)


@pytest.mark.parametrize("reply", [b"", b"1,2", b"1,NaN,2", b"1,2,3garbage"])
def test_invalid_ascii_never_returns_a_batch(reply):
    item = adapter()
    item.transport.values[":TRACE:DATA:Y? TRA"] = reply
    with pytest.raises(DeviceError) as error:
        item.acquire(AcquisitionRequest("spectrum"))
    assert error.value.code == "parse_error"


def test_empty_trace_is_no_data_and_does_not_query_payload():
    item = adapter()
    item.transport.values[":TRACE:DATA:SNUMBER? TRA"] = b"0"
    with pytest.raises(DeviceError) as error:
        item.acquire(AcquisitionRequest("spectrum"))
    assert error.value.code == "no_data"
    assert ":TRACE:DATA:X? TRA" not in item.transport.history


def test_complete_frame_configuration_change_is_distinct_from_parse_fault():
    item = adapter()

    def changed():
        item.transport.values[":SENSE:SENSE?"] = b"2"
        return b"-10,-20,-30"

    item.transport.values[":TRACE:DATA:Y? TRA"] = changed
    with pytest.raises(DeviceError) as error:
        item.acquire(AcquisitionRequest("spectrum"))
    assert error.value.code == "configuration_changed"


@pytest.mark.parametrize("order", ["<", ">"])
def test_frozen_ascii_real64_qualification_and_production_decoding(monkeypatch, order):
    item = adapter()
    ascii_xy = tuple(item.transport.values[f":TRACE:DATA:{axis}? TRA"] for axis in ("X", "Y"))
    binary_xy = tuple(
        ieee_block(np.asarray([float(v) for v in raw.split(b",")], dtype=order + "f8").tobytes())
        for raw in ascii_xy
    )
    evidence = qualify_binary(ascii_xy, binary_xy, 3, x_atol_nm=1e-6, y_atol_db=1e-6)
    assert evidence["byte_order"] == order
    monkeypatch.setitem(p.BINARY_QUALIFICATIONS, "2.1.0", order)
    item.prepare_acquisition()
    for axis, raw in zip(("X", "Y"), binary_xy, strict=True):
        item.transport.values[f":TRACE:DATA:{axis}? TRA"] = raw
    batch = item.acquire(AcquisitionRequest("spectrum"))
    np.testing.assert_allclose(batch.measurements[0].values, [-62.5, -12, -60.5])


@pytest.mark.parametrize("reply", [b"#18abc", b"#08abcdefgh", b"#18abcdefghGARBAGE"])
def test_binary_truncation_or_bad_framing_is_rejected(reply):
    with pytest.raises(DeviceError):
        p.values(reply, 1, "REAL,64", "<")


def test_real64_raw_payload_is_accepted_for_qualified_socket_framing():
    raw = np.asarray([1.54e-6], dtype="<f8").tobytes()
    np.testing.assert_allclose(p.values(raw, 1, "REAL,64", "<"), [1.54e-6])


def test_wrong_endian_is_not_automatically_retried():
    raw = ieee_block(np.asarray([1.54e-6, 1.56e-6], dtype="<f8").tobytes())
    with pytest.raises(DeviceError):
        p.wavelength_axis(raw, 2, "REAL,64", ">")


def test_restore_stops_first_failed_command_and_does_not_restart_scan():
    item = adapter()
    state = snapshot(item)
    item.transport.values[":SENSE:SWEEP:POINTS:AUTO?"] = b"1"
    with pytest.raises(DeviceError, match="points_auto"):
        restore(item, state, "stopped", "absent")
    writes = [c for c in item.transport.history if "?" not in c]
    assert writes == [":ABORT", ":SENSE:SWEEP:POINTS:AUTO OFF"]


@pytest.mark.parametrize(
    "current,target,expected",
    [
        (1600, (1540, 1560), ["start_wavelength", "stop_wavelength"]),
        (1500, (1540, 1560), ["stop_wavelength", "start_wavelength"]),
    ],
)
def test_restore_wavelength_dependency_order(current, target, expected):
    operations = []
    set_endpoints(lambda op, value: operations.append(op), lambda op: current, *target)
    assert operations == expected
