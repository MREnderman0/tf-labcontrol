import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError, DeviceIdentity
from lab_control.instruments.registry import build_scpi_adapter
from lab_control.transport.base import MockTransport


def device(exchanges, **kwargs):
    wire = MockTransport(exchanges)
    wire.open()
    config = ConnectionConfig("tcp://localhost:1299", "microchip.53100a", "tserve", **kwargs)
    adapter = build_scpi_adapter(config, wire)
    adapter.identity = DeviceIdentity("Microchip", "53100A", "TEST123")
    return adapter, wire


def measurement_exchanges(query, raw, final_frequency=b"10 MHz"):
    return [
        ("show state", b"Collecting"),
        ("show inputfreq", b"10 MHz"),
        ("show referencefreq", b"Auto"),
        (query, raw),
        ("show state", b"Collecting"),
        ("show inputfreq", final_frequency),
        ("show referencefreq", b"Auto"),
    ]


def test_identity_reads_serial_instead_of_copying_expected():
    adapter, wire = device(
        [
            ("show version", b"PhaseStation 53100A\r\nTSERVE 1.780A"),
            ("show serial", b"Serial number: ACTUAL123"),
        ],
        expected_serial="EXPECTED456",
    )
    with pytest.raises(DeviceError, match="序列号"):
        adapter.identify()
    assert wire.history == ["show version", "show serial"]


def test_identity_does_not_call_server_version_hardware_firmware():
    adapter, _ = device(
        [
            ("show version", b"PhaseStation 53100A TSERVE 1.780A"),
            ("show serial", b"TEST123"),
            ("show state", b"State: Ready"),
        ],
        expected_serial="TEST123",
    )
    identity = adapter.identify()
    assert identity.serial == "TEST123" and identity.firmware == ""
    assert adapter.capability_details["identity_evidence"] == "show serial"


@pytest.mark.parametrize("raw", [b"No instrument connected", b"UNKNOWN", b"", b"ERROR"])
def test_unavailable_identity_fails_closed(raw):
    adapter, _ = device([("show version", b"PhaseStation 53100A"), ("show serial", raw)])
    with pytest.raises(DeviceError):
        adapter.identify()


def test_frequency_write_readback_and_auto_restore():
    adapter, wire = device(
        [
            ("show inputfreq", b"auto"),
            ("show referencefreq", b"20 MHz"),
            ("show state", b"Ready"),
            ("set inputfreq 10", b""),
            ("show inputfreq", b"10 MHz"),
            ("show referencefreq", b"20 MHz"),
            ("show state", b"Ready"),
            ("set inputfreq auto", b""),
            ("show inputfreq", b"auto"),
        ]
    )
    initial = adapter.read_settings(("input_frequency", "reference_frequency"))
    result = adapter.apply(DeviceCommand("input_frequency", {"value": 10e6}))
    assert result.outcome == "success" and result.readback == {"automatic": False, "value": 10e6}
    assert adapter.read_settings(("reference_frequency",))[0].value == initial[1].value
    assert adapter.apply(DeviceCommand("input_frequency", {"automatic": True})).outcome == "success"
    assert not wire.exchanges


@pytest.mark.parametrize("value", [1, 999999, 200000001, float("nan")])
def test_range_rejected_before_io(value):
    adapter, wire = device([])
    with pytest.raises(DeviceError):
        adapter.apply(DeviceCommand("input_frequency", {"value": value}))
    assert not wire.history


def test_running_measurement_blocks_configuration_write():
    adapter, wire = device([("show state", b"State: Collecting")])
    with pytest.raises(DeviceError, match="Stop measurement"):
        adapter.apply(DeviceCommand("input_frequency", {"value": 10e6}))
    assert wire.history == ["show state"]


def test_write_timeout_is_uncertain_and_not_replayed():
    adapter, wire = device(
        [("show state", b"Ready"), ("set inputfreq 10", DeviceError("timeout", "timeout"))]
    )
    result = adapter.apply(DeviceCommand("input_frequency", {"value": 10e6}))
    assert result.outcome == "uncertain"
    assert wire.history == ["show state", "set inputfreq 10"]


def test_readback_mismatch_is_not_success():
    adapter, _ = device([("show state", b"Ready"), ("set inputfreq 10", b""), ("show inputfreq", b"20 MHz")])
    assert adapter.apply(DeviceCommand("input_frequency", {"value": 10e6})).outcome == "uncertain"


@pytest.mark.parametrize("command", [DeviceCommand("start"), DeviceCommand("stop")])
def test_start_and_stop_require_confirmation(command):
    adapter, wire = device([])
    with pytest.raises(DeviceError, match="confirmation"):
        adapter.apply(command)
    assert not wire.history


@pytest.mark.parametrize("raw", [b"1 -100\n2 nan", b"1 -100\nmalformed", b"2 -100\n1 -110", b"", b"0 -100"])
def test_invalid_spectrum_never_returns_partial_batch(raw):
    adapter, _ = device(measurement_exchanges("show spectrum", raw))
    with pytest.raises(DeviceError):
        adapter.acquire(AcquisitionRequest(quantity="phase_noise"))


def test_stopped_measurement_has_no_data():
    adapter, wire = device([("show state", b"Ready")])
    with pytest.raises(DeviceError):
        adapter.acquire(AcquisitionRequest(quantity="phase_noise"))
    assert wire.history == ["show state"]


def test_valid_adev():
    adapter, _ = device(measurement_exchanges("show adev", b"tau: 0.1 adev: 5e-12\ntau: 0.2 adev: 4e-12"))
    batch = adapter.acquire(AcquisitionRequest(quantity="stability"))
    assert batch.validation == "full" and batch.measurements[0].unit == "ratio"


def test_configuration_change_discards_complete_spectrum_without_retry():
    adapter, wire = device(measurement_exchanges("show spectrum", b"1 -100\n2 -110", b"20 MHz"))
    with pytest.raises(DeviceError) as caught:
        adapter.acquire(AcquisitionRequest(quantity="phase_noise"))
    assert caught.value.code == "configuration_changed"
    assert wire.history.count("show spectrum") == 1


def test_idle_service_maintenance_never_manufactures_identity_or_allows_start():
    adapter, _ = device(
        [
            ("show version", b"Model: Microchip 53100A\r\nSoftware: Version 1.78"),
            ("show state", b"Ready\r\nTime Constant: Infinite"),
        ]
    )
    adapter.identity = None
    assert adapter.inspect_service()["identity"] == "not_verified"
    assert adapter.identity is None
    with pytest.raises(DeviceError) as caught:
        adapter.apply(DeviceCommand("start", confirmed=True))
    assert caught.value.code == "identity_required"
