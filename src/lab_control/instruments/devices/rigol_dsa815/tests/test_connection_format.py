import pytest

from lab_control.domain.instruments import DeviceError

from .test_protocol import adapter


@pytest.mark.parametrize("order", ["NORM", "SWAP"])
def test_prepare_binary_once_preserves_order_and_already_binary_does_not_write(order):
    a = adapter()
    a.transport.exchanges.extend(
        [
            ("FORM?", b"ASCII"),
            ("FORM:BORD?", order.encode()),
            (":FORM:TRAC:DATA REAL,32", b""),
            ("*OPC?", b"1"),
            ("FORM?", b"REAL,32"),
            ("FORM:BORD?", order.encode()),
            ("SYST:ERR?", b'0,"No error"'),
        ]
    )
    a.prepare_acquisition()
    assert a.acquisition_preparation["status"] == "ready"
    assert a.acquisition_preparation["before"] == {"transfer_format": "ASC", "byte_order": order}
    assert a.acquisition_preparation["after"] == {"transfer_format": "REAL", "byte_order": order}
    assert sum("?" not in command for command in a.transport.history) == 1
    a.transport.history.clear()
    a.transport.exchanges.extend([("FORM?", b"REAL,32"), ("FORM:BORD?", order.encode())])
    a.prepare_acquisition()
    assert a.transport.history == ["FORM?", "FORM:BORD?"]
    assert not a.acquisition_preparation["format_write_attempted"]


@pytest.mark.parametrize("failure", ["timeout", "mismatch", "byte_order", "error"])
def test_prepare_failure_does_not_retry_or_fallback(failure):
    a = adapter()
    a.transport.exchanges.extend(
        [
            ("FORM?", b"ASCII"),
            ("FORM:BORD?", b"NORM"),
            (":FORM:TRAC:DATA REAL,32", b""),
            ("*OPC?", DeviceError("timeout", "timeout") if failure == "timeout" else b"1"),
        ]
    )
    if failure != "timeout":
        a.transport.exchanges.extend(
            [
                ("FORM?", b"ASCII" if failure == "mismatch" else b"REAL,32"),
                ("FORM:BORD?", b"SWAP" if failure == "byte_order" else b"NORM"),
                ("SYST:ERR?", b'-100,"Command error"' if failure == "error" else b'0,"No error"'),
            ]
        )
    with pytest.raises(DeviceError, match="未自动重试"):
        a.prepare_acquisition()
    assert a.acquisition_preparation["status"] == "failed"
    assert a.acquisition_preparation["format_write_attempted"]
    assert sum("?" not in command for command in a.transport.history) == 1
    assert not a.transport.exchanges


def test_unidentified_adapter_never_configures_format():
    a = adapter()
    a.identity = None
    with pytest.raises(DeviceError):
        a.prepare_acquisition()
    assert not a.transport.history
