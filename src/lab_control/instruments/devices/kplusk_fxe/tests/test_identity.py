import pytest

from lab_control.domain.instruments import AcquisitionRequest, DeviceError
from lab_control.instruments.devices.kplusk_fxe.adapter import KpluskFxeAdapter
from lab_control.instruments.devices.kplusk_fxe.identity import parse_version_identity
from lab_control.instruments.devices.kplusk_fxe.tests.test_adapter import (
    BinaryReport,
    FakeLibrary,
    LogReport,
    Result,
    config,
)


@pytest.mark.parametrize("prefix", ["7001", "7001; ", ""])
@pytest.mark.parametrize("model", ["FXE", "FXE2"])
def test_version_identity_uses_model_and_serial_not_old_ip(prefix, model):
    raw = f"{prefix}VERSION {model} / # TEST-517 User 1 / E20-1.19 / IP: 198.51.100.20"
    identity = parse_version_identity(raw)
    assert (identity.model, identity.serial, identity.firmware) == (model, "TEST-517", "E20-1.19")
    assert identity.raw == raw and identity.confidence == "protocol"


@pytest.mark.parametrize(
    "raw",
    [
        "7022; Buffer cleared",
        "VERSION FXE / FW: 70",
        "VERSION FXE3 / # TEST User 1 / FW",
        "VERSION FXE2 / # 00000000 User 1 / FW",
    ],
)
def test_unverified_identity_rejected(raw):
    with pytest.raises(DeviceError) as exc:
        parse_version_identity(raw)
    assert exc.value.code == "identity_required"


def test_identity_query_once_and_interleaved_frames_preserved(fake_clock):
    class InterleavedLibrary(FakeLibrary):
        def send_command(self, source, command):
            result = super().send_command(source, command)
            self.binary_reports.insert(0, BinaryReport((10.0, 20.0)))
            return result

    library = InterleavedLibrary()
    adapter = KpluskFxeAdapter(config(), library)
    assert adapter.identify() is adapter.identify()
    assert library.commands == [b"\x81"]
    batch = adapter.acquire(AcquisitionRequest(kind="scalar", channels=(2,), quantity="frequency"))
    assert batch.measurements[0].values == (20.0,)
    adapter.close()
    assert library.closed == [7]


@pytest.mark.parametrize("busy", [False, True])
def test_identity_timeout_bounded_without_replay(fake_clock, busy):
    class MissingVersion(FakeLibrary):
        def send_command(self, source, command):
            self.commands.append(command)
            return Result()

        def get_kk_report(self, source):
            return LogReport("7015; timestamp") if busy else super().get_kk_report(source)

    library = MissingVersion()
    adapter = KpluskFxeAdapter(config(), library)
    with pytest.raises(DeviceError) as exc:
        adapter.identify()
    assert exc.value.code == "timeout"
    assert fake_clock.now == pytest.approx(adapter.config.timeout)
    assert library.commands == [b"\x81"]
    adapter.close()
