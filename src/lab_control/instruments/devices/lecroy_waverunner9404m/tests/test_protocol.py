import struct

import numpy as np
import pytest

from lab_control.domain.instruments import DeviceError

from ..protocol import choice, numeric, transfer_setup, waveform
from .samples import binary


@pytest.mark.parametrize(
    "raw,expected",
    [
        (b"TDIV 1e-3 S", 0.001),
        (b"C2:VDIV 50MV", 0.05),
        (b"MSIZ 1MA", 1e6),
        (b"MSIZ 500E+3 SAMPLE", 500000),
        (b"MSIZ 10KSAMPLE", 10000),
        (b"-2E-3V", -0.002),
        (b"1.0M", 0.001),
    ],
)
def test_scalar_units(raw, expected):
    assert numeric(raw) == expected


@pytest.mark.parametrize("raw", [b"", b"NaN", b"1e400", b"1;*RST", b"1V\n2V", b"C1:VDIV 3BAD"])
def test_invalid_scalar_is_not_state(raw):
    with pytest.raises(DeviceError):
        numeric(raw)


def test_remote_interface_warning_is_actionable():
    for parse in (numeric, transfer_setup, lambda raw: waveform(raw, 1)):
        with pytest.raises(DeviceError) as error:
            parse(b"WARNING : CURRENT REMOTE CONTROL INTERFACE IS TCPIP\n")
        assert error.value.code == "remote_interface_mismatch"


def test_enum_and_transfer_setup():
    assert choice(b"C1:TRA OFF", choices=("ON", "OFF")) == "OFF"
    with pytest.raises(DeviceError):
        choice(b"OFFLINE", choices=("ON", "OFF"))
    assert transfer_setup(b"WFSU SP,1,NP,1000,FP,0,SN,0")["NP"] == 1000
    with pytest.raises(DeviceError):
        transfer_setup(b"SP,0,SP,0,FP,0,SN,0")


@pytest.mark.parametrize("endian", ["<", ">"])
@pytest.mark.parametrize("width", [0, 1])
def test_waveform_scaling_both_orders_widths(endian, width):
    measured = waveform(binary(3, endian, width), 3)
    np.testing.assert_allclose(measured.values, [-0.04, -0.02, -0.01, 0.01], atol=1e-8)
    np.testing.assert_allclose(measured.axis, [-0.001, 0, 0.001, 0.002], atol=1e-9)
    assert measured.channel == 3 and measured.unit == "V" and measured.axis_unit == "s"


@pytest.mark.parametrize(
    "offset,fmt,value",
    [
        (36, "i", 100),
        (60, "i", 99),
        (116, "i", -1),
        (124, "i", -1),
        (128, "i", 10),
        (156, "f", float("nan")),
        (176, "f", 0),
        (180, "d", float("inf")),
        (34, "h", 2),
        (32, "h", 3),
    ],
)
def test_corrupt_descriptor_rejected(offset, fmt, value):
    with pytest.raises(DeviceError) as error:
        waveform(binary(**{str(offset): (fmt, value)}), 1)
    assert error.value.code == "parse_error"


@pytest.mark.parametrize("offset,fmt,value", [(144, "i", 2), (316, "h", 3), (318, "h", 5), (344, "h", 2)])
def test_unqualified_waveforms_rejected(offset, fmt, value):
    with pytest.raises(DeviceError) as error:
        waveform(binary(**{str(offset): (fmt, value)}), 1)
    assert error.value.code == "unsupported"


def test_partial_trailing_wrong_channel_and_template():
    data = binary()
    for invalid in (
        data[:-5],
        data + b"unexpected",
        data.replace(b"C1:", b"C4:", 1),
        data.replace(b"LECROY_2_3", b"OTHER__9_9"),
    ):
        with pytest.raises(DeviceError):
            waveform(invalid, 1)
    # A descriptor with a second array cannot be smuggled through a larger block.
    raw = bytearray(data)
    base = raw.index(b"WAVEDESC")
    struct.pack_into("<i", raw, base + 64, 2)
    with pytest.raises(DeviceError):
        waveform(bytes(raw), 1)
