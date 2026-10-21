from __future__ import annotations

import struct

import pytest

from lab_control.domain.instruments import DeviceError, ValidationError
from lab_control.instruments.devices.kplusk_fxe.protocol import (
    parse_frequency_report,
    parse_phase_report,
    validate_channels,
)


class Report:
    def __init__(self, report_type=4, header=0x2900, values=(1.0, 2.0), content=None):
        self.report_type = report_type
        self.header = header
        self.values = values
        self.content = (
            struct.pack("<" + "d" * len(values), *values) if content is None else content
        )

    def get_report_type(self):
        return self.report_type

    def get_header(self):
        return self.header

    def get_channel_count(self):
        return len(self.values)

    def get_content(self):
        return self.content

    def get_device_ms(self):
        return 99


class PhaseReport(Report):
    def __init__(self, values=(1.0, 2.0), device_ms=99, header=0x19C0):
        content = b"".join(struct.pack("<qd", int(value), value - int(value)) for value in values)
        super().__init__(report_type=3, header=header, values=values, content=content)
        self.device_ms = device_ms

    def get_device_ms(self):
        return self.device_ms


def test_frequency_report_decodes_little_endian_doubles_and_interval():
    parsed = parse_frequency_report(Report(values=(10e6, 20e6)))
    assert parsed is not None
    assert parsed.values == (10e6, 20e6)
    assert parsed.device_ms == 99
    assert parsed.interval_ms == 1000


def test_phase_report_decodes_period_counts_without_vendor_import():
    parsed = parse_phase_report(PhaseReport(values=(100.25, 200.5), device_ms=500))
    assert parsed is not None
    assert parsed.values == (100.25, 200.5)
    assert parsed.interval_ms == 1000


@pytest.mark.parametrize("report", [Report(report_type=0), Report(report_type=2), Report(header=0x1900)])
def test_non_frequency_reports_are_ignored(report):
    assert parse_frequency_report(report) is None


def test_invalid_or_nonfinite_frequency_report_is_rejected():
    with pytest.raises(DeviceError, match="truncated"):
        parse_frequency_report(Report(values=(1.0,), content=b"short"))
    with pytest.raises(DeviceError, match="non-finite"):
        parse_frequency_report(Report(values=(float("nan"),)))


def test_channels_are_bounded_and_unique():
    assert validate_channels((1, 24)) == (1, 24)
    with pytest.raises(ValidationError):
        validate_channels((0,))
    with pytest.raises(ValidationError):
        validate_channels((1, 1))
