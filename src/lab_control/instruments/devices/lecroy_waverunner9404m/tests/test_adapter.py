import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError, DeviceIdentity
from lab_control.instruments.registry import build_scpi_adapter
from lab_control.transport.base import MockTransport

from ..adapter import TRANSFER_SETTINGS, WaveRunner9404MAdapter
from ..profile import profile
from .samples import GLOBALS, binary, snapshot_exchanges


def device(exchanges=()):
    transport = MockTransport(exchanges)
    transport.open()
    adapter = build_scpi_adapter(ConnectionConfig("mock://fixture", "lecroy.9404m"), transport)
    assert isinstance(adapter, WaveRunner9404MAdapter)
    adapter.identity = DeviceIdentity("LECROY", "WAVERUNNER9404M", "TEST", "9.2.0")
    return adapter, transport


def transfer_reads(fmt=b"CFMT DEF9,WORD,BIN"):
    p = profile()
    return [
        (p.commands[key].query, fmt if key == "transfer_format" else GLOBALS[key])
        for key in TRANSFER_SETTINGS
    ]


@pytest.mark.parametrize("header", [b"", b"*IDN "])
def test_identity_with_and_without_response_header(header):
    adapter, _ = device([("*IDN?", header + b"LECROY,WAVERUNNER9404M,TEST,9.2.0")])
    identity = adapter.identify()
    assert identity.manufacturer == "LECROY" and identity.serial == "TEST"


def test_other_model_is_rejected_before_configuration():
    adapter, wire = device([("*IDN?", b"LECROY,WAVERUNNER9254,TEST,9.2.0")])
    with pytest.raises(DeviceError) as error:
        adapter.identify()
    assert error.value.code == "identity_conflict" and wire.history == ["*IDN?"]


@pytest.mark.parametrize("already_word", [False, True])
def test_prepare_once_only_if_needed_and_preserves_other_settings(already_word):
    before = b"CFMT DEF9,WORD,BIN" if already_word else b"CFMT DEF9,BYTE,BIN"
    writes = [] if already_word else [("CFMT DEF9,WORD,BIN", b"")]
    adapter, transport = device(transfer_reads(before) + writes + transfer_reads())
    adapter.prepare_acquisition()
    assert adapter.acquisition_preparation["status"] == "ready"
    assert sum(cmd == "CFMT DEF9,WORD,BIN" for cmd in transport.history) == int(not already_word)
    assert not transport.exchanges


def test_failed_preparation_is_not_replayed():
    adapter, transport = device(
        transfer_reads(b"CFMT DEF9,BYTE,BIN")
        + [("CFMT DEF9,WORD,BIN", b"")]
        + transfer_reads(b"CFMT DEF9,BYTE,BIN")
    )
    with pytest.raises(DeviceError):
        adapter.prepare_acquisition()
    assert adapter.acquisition_preparation["status"] == "failed"
    assert transport.history.count("CFMT DEF9,WORD,BIN") == 1


def test_complete_frame_no_setting_writes_and_settings_timestamps():
    p = profile()
    snapshot = snapshot_exchanges(p, (1, 2))
    adapter, transport = device(snapshot + [("C1:WF? ALL", binary()), ("C2:WF? ALL", binary(2))] + snapshot)
    batch = adapter.acquire(AcquisitionRequest("waveform", (1, 2)))
    assert batch.validation == "full" and len(batch.measurements) == 2
    assert batch.settings_timestamps.keys() == batch.configuration.keys()
    assert all("?" in cmd for cmd in transport.history)
    assert all(r.source == "instrument" for r in adapter.acquisition_readings(batch))


def test_complete_frame_changed_settings_are_distinct_from_parse_failure():
    p = profile()
    before = snapshot_exchanges(p)
    after = snapshot_exchanges(p, overrides={"1.scale": b"0.05 V"})
    adapter, _ = device(before + [("C1:WF? ALL", binary())] + after)
    with pytest.raises(DeviceError) as error:
        adapter.acquire(AcquisitionRequest("waveform"))
    assert error.value.code == "configuration_changed" and '"1.scale"' in str(error.value)
    adapter, _ = device(before + [("C1:WF? ALL", binary()[:-5])])
    with pytest.raises(DeviceError) as error:
        adapter.acquire(AcquisitionRequest("waveform"))
    assert error.value.code == "parse_error"


def test_disabled_channel_and_unprepared_transfer_cannot_start_or_write():
    for override in ({"1.channel_enabled": b"OFF"}, {"1.transfer_format": b"DEF9,BYTE,BIN"}):
        adapter, transport = device(snapshot_exchanges(profile(), overrides=override))
        with pytest.raises(DeviceError):
            adapter.acquire(AcquisitionRequest("waveform"))
        assert all("?" in command for command in transport.history)


def test_input_coupling_limit_enforced_before_write():
    adapter, transport = device(
        [("C2:CPL?", b"D50"), ("C2:ATTN?", b"1"), ("C2:VDIV?", b".02 V"), ("C2:OFST?", b"0 V")]
    )
    with pytest.raises(DeviceError):
        adapter.apply(DeviceCommand("scale", {"value": 2}, 2))
    assert all("?" in command for command in transport.history)


def test_tiny_timebase_mismatch_is_not_hidden_by_common_absolute_tolerance():
    adapter, _ = device([("TDIV 1e-10", b""), ("*OPC?", b"1"), ("TDIV?", b"TDIV 2e-10 S")])
    assert adapter.apply(DeviceCommand("timebase", {"value": 1e-10})).outcome == "rejected"


@pytest.mark.parametrize("operation,expected", [("run", "AUTO"), ("stop", "STOP"), ("single", "SINGLE")])
def test_trigger_commands_read_back_without_opc_or_replay(operation, expected):
    adapter, transport = device([("TRMD " + expected, b""), ("TRMD?", expected.encode())])
    result = adapter.apply(DeviceCommand(operation, confirmed=True))
    assert result.outcome == "success" and result.readback == expected
    assert len(transport.history) == 2
