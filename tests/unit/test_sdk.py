import pytest

from lab_control.domain.instruments import DeviceError
from lab_control.transport.sdk import VendorLibrary


def test_missing_dll_reports_required_absolute_path():
    with pytest.raises(DeviceError, match="absolute SDK"):
        VendorLibrary("no-such-driver.dll")
