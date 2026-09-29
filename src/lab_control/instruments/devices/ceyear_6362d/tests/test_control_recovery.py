import pytest

from lab_control.domain.instruments import DeviceError
from lab_control.instruments.devices.ceyear_6362d.hardware.state import (
    prepare_control_traces,
    restore,
    snapshot,
    stop_scan,
    trace_attributes,
    validate_backup,
)
from lab_control.instruments.devices.ceyear_6362d.tests.test_contract import Instrument, adapter


class TraceInstrument(Instrument):
    def __init__(self):
        super().__init__()
        self.values.update({f":TRACE:ATTRIBUTE:TR{letter}?": b"1" for letter in "ABCDEFG"})
        self.values[":TRACE:ATTRIBUTE:TRB?"] = b"0"
        self.values[":TRACE:ACTIVE?"] = b"TRB"

    def write(self, command):
        super().write(command)
        if command.startswith(":TRACE:ATTRIBUTE:"):
            header, value = command.split()
            self.values[header + "?"] = b"0" if value == "WRITE" else b"1"
            # B.11: changing any trace attribute also selects that trace.
            self.values[":TRACE:ACTIVE?"] = header.rsplit(":", 1)[1].encode()
        if command.startswith(":TRACE:ACTIVE "):
            self.values[":TRACE:ACTIVE?"] = command.split()[1].encode()


def test_explicit_leave_stopped_waives_running_state_but_retains_parameter_guards():
    state = snapshot(adapter(TraceInstrument()))
    with pytest.raises(DeviceError, match="Panel-confirmed"):
        validate_backup(state, "unknown", "unknown")
    validate_backup(state, "unknown", "unknown", leave_stopped=True)
    with pytest.raises(DeviceError, match="restoration envelope"):
        validate_backup(dict(state, sweep_mode="AUTO"), "unknown", "unknown", leave_stopped=True)


def test_stop_uses_abort_before_queries_blocked_by_pending_single_scan():
    item = adapter()
    item.transport.history.clear()
    result = stop_scan(item)
    assert item.transport.history[:2] == [":ABORT", ":STATUS:OPERATION:CONDITION?"]
    assert result["condition"] == 1 and not result["restarted"]


def test_held_tra_and_writing_trb_are_restored_without_trigger_change_or_scan_restart():
    item = adapter(TraceInstrument())
    state, attributes = snapshot(item), trace_attributes(item)
    prepare_control_traces(item, attributes)
    assert item.read("trace_mode") == "WRITE"
    assert trace_attributes(item)["TRB"] == 1
    result = restore(item, state, "unknown", "unknown", leave_stopped=True, attributes=attributes)
    assert snapshot(item) == state
    assert trace_attributes(item) == attributes
    assert result["scan_command"] == "leave_stopped"
    assert not result["historical_curves_restored"]
    assert not any(c.startswith(":TRIGGER") or c == ":INITIATE" for c in item.transport.history)


def test_unrestorable_trace_average_rejects_before_any_mutation():
    item = adapter(TraceInstrument())
    attributes = trace_attributes(item)
    attributes["TRC"] = 4
    item.transport.history.clear()
    with pytest.raises(DeviceError, match="seven WRITE/FIX"):
        prepare_control_traces(item, attributes)
    assert not item.transport.history
