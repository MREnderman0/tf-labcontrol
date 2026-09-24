from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import DeviceCommand
from lab_control.instruments.registry import build_scpi_adapter
from lab_control.transport.base import MockTransport


def adapter(profile_id, exchanges):
    transport = MockTransport(exchanges)
    transport.open()
    return build_scpi_adapter(ConnectionConfig("mock://fixture", profile_id), transport), transport


def test_sma_list_upload_is_three_ordered_writes_and_never_starts_output():
    exchanges = [
        ("SOUR:FREQ:MODE?", b"CW"),
        ("SOUR:LIST:CAT?", b"original"),
        ("SOUR:LIST:SEL 'trial'", b""),
        ("SOUR:LIST:FREQ 1000000,2000000", b""),
        ("SOUR:LIST:POW -40,-30", b""),
        ("SYST:ERR?", b"0,No error"),
        ("FORM?", b"ASC"),
        ("SOUR:LIST:SEL?", b"/var/user/trial.lsw"),
        ("SOUR:LIST:FREQ:POIN?", b"2"),
        ("SOUR:LIST:FREQ?", b"1000000,2000000"),
        ("SOUR:LIST:POW:POIN?", b"2"),
        ("SOUR:LIST:POW?", b"-40,-30"),
    ]
    device, transport = adapter("rs.sma100b", exchanges)
    result = device.apply(
        DeviceCommand(
            "upload_list", {"name": "trial", "frequencies": [1e6, 2e6], "powers": [-40, -30]}, confirmed=True
        )
    )
    assert result.outcome == "success" and result.completed_steps == 3
    assert not transport.exchanges
