from __future__ import annotations

import struct
from dataclasses import dataclass

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError, Unsupported
from lab_control.instruments.devices.kplusk_fxe.adapter import KpluskFxeAdapter


@dataclass
class Result:
    result_code: str = "KK_NO_ERR"
    data: str | None = None


class BinaryReport:
    def __init__(self, values: tuple[float, ...], header: int = 0x2900, device_ms: int = 1234) -> None:
        self.values = values
        self.header = header
        self.device_ms = device_ms

    def get_report_type(self):
        return 4

    def get_header(self) -> int:
        return self.header

    def get_channel_count(self) -> int:
        return len(self.values)

    def get_content(self) -> bytes:
        return struct.pack("<" + "d" * len(self.values), *self.values)

    def get_device_ms(self) -> int:
        return self.device_ms

    def to_log_str(self) -> str:
        return f"{self.header:04x}; " + ";".join(str(value) for value in self.values)


class PhaseReport:
    def __init__(self, values: tuple[float, ...], device_ms: int, header: int = 0x19C0) -> None:
        self.values = values
        self.device_ms = device_ms
        self.header = header

    def get_report_type(self):
        return 3

    def get_header(self) -> int:
        return self.header

    def get_channel_count(self) -> int:
        return len(self.values)

    def get_content(self) -> bytes:
        return b"".join(struct.pack("<qd", int(value), value - int(value)) for value in self.values)

    def get_device_ms(self) -> int:
        return self.device_ms

    def to_log_str(self) -> str:
        return f"{self.header:04x}; " + ";".join(str(value) for value in self.values)


class LogReport:
    def __init__(self, text: str) -> None:
        self.text = text

    def get_report_type(self):
        return 2

    def to_log_str(self) -> str:
        return self.text


class FakeLibrary:
    def __init__(self, reports: list[str | None] | None = None) -> None:
        self.reports = list(reports or [])
        self.opened: tuple[int, str, bool] | None = None
        self.closed: list[int] = []
        self._source = 7
        self.binary_reports: list[object] = []

    def get_source_id(self) -> int:
        return self._source

    def open_connection(self, source: int, resource: str, blocking: bool) -> Result:
        self.opened = (source, resource, blocking)
        return Result()

    def close_connection(self, source: int) -> None:
        self.closed.append(source)

    def get_firmware_version(self, source: int) -> int:
        assert source == self._source
        return 70

    def get_user_id(self, source: int) -> int:
        assert source == self._source
        return 1

    def get_report(self, source: int) -> Result:
        assert source == self._source
        return Result(data=self.reports.pop(0) if self.reports else None)

    def get_kk_report(self, source: int):
        assert source == self._source
        return self.binary_reports.pop(0) if self.binary_reports else type("Empty", (), {
            "get_report_type": lambda self: 0,
        })()


def config(resource: str = "192.0.2.10") -> ConnectionConfig:
    return ConnectionConfig(resource, "kplusk.fxe", "kplusk")


def test_factory_opens_official_direct_ip_without_writes() -> None:
    library = FakeLibrary(["7015; 123", "1900; raw"])
    library.binary_reports = [LogReport("7015; 123"), LogReport("1900; raw")]
    adapter = KpluskFxeAdapter(config(), library)

    assert library.opened == (7, "192.0.2.10", False)
    identity = adapter.identify()
    assert identity.manufacturer == "K+K"
    assert identity.model == "FXE"
    status = adapter.read_status()
    assert status.values["reports"] == ("7015; 123", "1900; raw")
    assert status.values["report_interpretation"] == "raw_protocol_only"
    adapter.close()
    assert library.closed == [7]


def test_kplusk_url_and_no_setting_replay() -> None:
    library = FakeLibrary()
    adapter = KpluskFxeAdapter(ConnectionConfig("kplusk://192.0.2.10:1234", "kplusk.fxe", "kplusk"), library)
    assert library.opened == (7, "192.0.2.10:1234", False)
    with pytest.raises(Unsupported):
        adapter.apply(DeviceCommand("firmware_version"))
    assert library.reports == []
    adapter.close()


def test_empty_report_is_not_saved_as_measurement() -> None:
    library = FakeLibrary([None])
    adapter = KpluskFxeAdapter(config(), library)
    reading = adapter.read_settings(("report",))[0]
    assert reading.value == ()
    assert reading.source == "instrument"
    with pytest.raises(DeviceError, match="frequency report"):
        adapter.acquire(AcquisitionRequest(kind="scalar", quantity="frequency"))
    adapter.close()


def test_binary_frequency_acquisition_filters_channels_without_writes() -> None:
    library = FakeLibrary()
    library.binary_reports = [BinaryReport((10_000_000.0, 20_000_000.0, 30_000_000.0))]
    adapter = KpluskFxeAdapter(config(), library)
    batch = adapter.acquire(AcquisitionRequest(kind="scalar", channels=(1, 3), quantity="frequency"))
    assert [(m.channel, m.quantity, m.unit, m.values[0]) for m in batch.measurements] == [
        (1, "frequency", "Hz", 10_000_000.0),
        (3, "frequency", "Hz", 30_000_000.0),
    ]
    assert batch.configuration["read_only"] is True
    assert batch.performance["protocol"] == "Multi_GetReportStruct"
    adapter.close()


def test_phase_average_reports_produce_read_only_frequency_delta() -> None:
    library = FakeLibrary()
    library.binary_reports = [PhaseReport((1_000_000.25, 2_000_000.5), 1000)]
    adapter = KpluskFxeAdapter(config(), library)
    with pytest.raises(DeviceError, match="frequency report"):
        adapter.acquire(AcquisitionRequest(kind="scalar", channels=(1, 2), quantity="frequency"))
    library.binary_reports.append(PhaseReport((11_000_000.25, 22_000_000.5), 2000))
    batch = adapter.acquire(AcquisitionRequest(kind="scalar", channels=(1, 2), quantity="frequency"))
    assert [measurement.values[0] for measurement in batch.measurements] == [10_000_000.0, 20_000_000.0]
    assert batch.configuration["derived_from_phase"] is True
    assert batch.configuration["source_report_type"] == "RT_PHASE"
    adapter.close()
