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
        return (
            self.binary_reports.pop(0)
            if self.binary_reports
            else type(
                "Empty",
                (),
                {
                    "get_report_type": lambda self: 0,
                },
            )()
        )


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


def test_factory_accepts_saved_visa_socket_resource_for_address_list() -> None:
    library = FakeLibrary()
    adapter = KpluskFxeAdapter(
        ConnectionConfig("TCPIP0::192.0.2.10::10001::SOCKET", "kplusk.fxe", "kplusk"),
        library,
    )
    assert library.opened == (7, "192.0.2.10", False)
    adapter.close()


def test_kplusk_url_and_no_setting_replay() -> None:
    library = FakeLibrary()
    adapter = KpluskFxeAdapter(ConnectionConfig("kplusk://192.0.2.10:1234", "kplusk.fxe", "kplusk"), library)
    assert library.opened == (7, "192.0.2.10:1234", False)
    with pytest.raises(Unsupported):
        adapter.apply(DeviceCommand("firmware_version"))
    assert library.reports == []
    adapter.close()


def test_empty_report_is_not_saved_as_measurement(fake_clock) -> None:
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


def test_phase_average_reports_produce_read_only_frequency_delta(fake_clock) -> None:
    library = FakeLibrary()
    library.binary_reports = [
        PhaseReport((1_000_000.25, 2_000_000.5), 1000),
        PhaseReport((11_000_000.25, 22_000_000.5), 2000),
    ]
    adapter = KpluskFxeAdapter(config(), library)
    batch = adapter.acquire(AcquisitionRequest(kind="scalar", channels=(1, 2), quantity="frequency"))
    assert [measurement.values[0] for measurement in batch.measurements] == [10_000_000.0, 20_000_000.0]
    assert batch.configuration["derived_from_phase"] is True
    assert batch.configuration["source_report_type"] == "RT_PHASE"
    adapter.close()


def test_many_messages_do_not_exhaust_wait_and_channels_are_detected(fake_clock):
    library = FakeLibrary()
    library.binary_reports = [LogReport("7015; timestamp") for _ in range(150)] + [
        BinaryReport((1.0, 2.0, 3.0, 4.0))
    ]
    adapter = KpluskFxeAdapter(config(), library)
    assert adapter.capabilities().channels == ()
    adapter.refresh_capabilities()
    assert adapter.capabilities().channels == (1, 2, 3, 4)
    assert adapter.capability_details["detected_channel_count"] == 4
    batch = adapter.acquire(AcquisitionRequest(kind="scalar", channels=(1, 4), quantity="frequency"))
    assert [m.values[0] for m in batch.measurements] == [1.0, 4.0]
    assert fake_clock.now < 1


def test_status_retains_measurements_and_metadata_does_not_poll(fake_clock):
    library = FakeLibrary()
    library.binary_reports = [BinaryReport((1.0, 2.0, 3.0, 4.0))]
    adapter = KpluskFxeAdapter(config(), library)
    adapter.read_settings(("firmware_version", "user_id"))
    assert len(library.binary_reports) == 1
    adapter.read_status()
    assert not library.binary_reports and len(adapter.reports.pending) == 1
    batch = adapter.acquire(AcquisitionRequest(kind="scalar", channels=(1, 4), quantity="frequency"))
    assert len(batch.measurements) == 2


def test_missing_selected_channel_fails_whole_frame(fake_clock):
    library = FakeLibrary()
    library.binary_reports = [BinaryReport((1.0, 2.0, 3.0, 4.0))]
    adapter = KpluskFxeAdapter(config(), library)
    with pytest.raises(DeviceError, match="所选通道不存在") as exc:
        adapter.acquire(AcquisitionRequest(kind="scalar", channels=(1, 5), quantity="frequency"))
    assert exc.value.code == "invalid_parameter"


def test_delayed_initial_phase_pair_waits_without_retry(fake_clock):
    class DelayedLibrary(FakeLibrary):
        def get_kk_report(self, source):
            if fake_clock.now >= 7 and not self.binary_reports:
                self.binary_reports = [PhaseReport((100.0,), 1000), PhaseReport((200.0,), 2000)]
            return super().get_kk_report(source)

    adapter = KpluskFxeAdapter(config(), DelayedLibrary())
    batch = adapter.acquire(AcquisitionRequest(kind="scalar", channels=(1,), quantity="frequency"))
    assert batch.measurements[0].values[0] == 100
    assert 7 <= fake_clock.now < 8


@pytest.mark.parametrize("timestamp,header", [(1000, 0x19C0), (0, 0x19C0), (2000, 0x1900)])
def test_phase_discontinuity_requires_new_baseline(fake_clock, timestamp, header):
    library = FakeLibrary()
    library.binary_reports = [PhaseReport((100.0,), 1000), PhaseReport((110.0,), timestamp, header)]
    adapter = KpluskFxeAdapter(config(), library)
    with pytest.raises(DeviceError, match="frequency report"):
        adapter.acquire(AcquisitionRequest(kind="scalar", channels=(1,), quantity="frequency"))
    assert fake_clock.now == pytest.approx(30)


def test_report_queue_overflow_is_explicit(fake_clock):
    library = FakeLibrary()
    library.binary_reports = [BinaryReport((1.0,)) for _ in range(65)]
    adapter = KpluskFxeAdapter(config(), library)
    for _ in range(8):
        adapter.read_status()
    with pytest.raises(DeviceError) as exc:
        adapter.read_status()
    assert exc.value.code == "buffer_overflow"
    assert len(adapter.reports.pending) == 64


def test_large_phase_integer_preserves_fractional_frequency(fake_clock):
    class PrecisePhase(PhaseReport):
        def __init__(self, high, low, device_ms):
            super().__init__((0.0,), device_ms)
            self.high, self.low = high, low

        def get_content(self):
            return struct.pack("<qd", self.high, self.low)

    library = FakeLibrary()
    library.binary_reports = [PrecisePhase(2**60, 0.25, 1000), PrecisePhase(2**60 + 10, 0.75, 2000)]
    adapter = KpluskFxeAdapter(config(), library)
    batch = adapter.acquire(AcquisitionRequest(kind="scalar", channels=(1,), quantity="frequency"))
    assert batch.measurements[0].values[0] == 10.5


def test_real_error_is_not_reported_as_wait_timeout(fake_clock):
    class ErrorReport(LogReport):
        def get_report_type(self):
            return 1

        def get_error_text(self):
            return "device disconnected"

    library = FakeLibrary()
    library.binary_reports = [ErrorReport("device disconnected")]
    adapter = KpluskFxeAdapter(config(), library)
    with pytest.raises(DeviceError, match="device disconnected") as exc:
        adapter.acquire(AcquisitionRequest(kind="scalar", channels=(1,), quantity="frequency"))
    assert exc.value.code == "device_error" and fake_clock.now == 0
