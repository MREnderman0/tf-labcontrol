import pytest

from lab_control.domain.instruments import DeviceError, ValidationError
from lab_control.transport.scpi import scalar_query_message, scalar_response_fields


def test_queries_are_rooted_and_reply_fields_are_exact():
    assert scalar_query_message(("SENS:FREQ:STAR?", ":FORM?", "*OPC?")) == ":SENS:FREQ:STAR?;:FORM?;*OPC?"
    assert scalar_response_fields(b" 1e3 ;REAL,32;ON\r\n", 3) == (b"1e3", b"REAL,32", b"ON")


@pytest.mark.parametrize(
    "queries", [(), ("*RST",), ("FREQ?;*RST",), ("FREQ?\n",), ("TRAC? TRACE1",), ("*IDN?",) * 65]
)
def test_no_settings_arguments_or_injected_messages(queries):
    with pytest.raises(ValidationError):
        scalar_query_message(queries)


@pytest.mark.parametrize(
    "raw", [b"1", b"1;2;3", b"1;", b"1;;", b'1;"2"', b"1;#112", b"1;2\n3", b"1;\xff", b"1;2\n\n"]
)
def test_incomplete_extra_binary_and_multiline_replies_are_rejected(raw):
    with pytest.raises(DeviceError) as caught:
        scalar_response_fields(raw, 2)
    assert caught.value.code == "parse_error"
