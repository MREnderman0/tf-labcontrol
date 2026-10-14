from __future__ import annotations

from dataclasses import dataclass

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, Unsupported
from lab_control.instruments.devices.kplusk_fxe.adapter import KpluskFxeAdapter


@dataclass
class Result:
    result_code: str = "KK_NO_ERR"
    data: str | None = None


class FakeLibrary:
    def __init__(self, reports: list[str | None] | None = None) -> None:
        self.reports = list(reports or [])
        self.opened: tuple[int, str, bool] | None = None
        self.closed: list[int] = []
        self._source = 7

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


def config(resource: str = "192.0.2.10") -> ConnectionConfig:
    return ConnectionConfig(resource, "kplusk.fxe", "kplusk")


def test_factory_opens_official_direct_ip_without_writes() -> None:
    library = FakeLibrary(["7015; 123", "1900; raw"])
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
    with pytest.raises(Unsupported):
        adapter.acquire(AcquisitionRequest())
    adapter.close()
