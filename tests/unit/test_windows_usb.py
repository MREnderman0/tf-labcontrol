import pytest

from lab_control.instruments.common import windows_usb


def test_pnp_metadata_filters_exact_ids_and_retains_missing_driver(monkeypatch):
    monkeypatch.setattr(windows_usb.sys, "platform", "win32")
    row = {"PNPDeviceID": r"USB\VID_16D0&PID_05B8\TEST", "ConfigManagerErrorCode": 28}
    monkeypatch.setattr(
        windows_usb, "_present_devices", lambda: [row, {"PNPDeviceID": r"USB\VID_16D0&PID_05B80\OTHER"}]
    )
    assert windows_usb.enumerate_usb("16d0", "05b8") == (row,)


def test_invalid_usb_filter_is_rejected_before_system_access(monkeypatch):
    monkeypatch.setattr(
        windows_usb, "_present_devices", lambda: pytest.fail("Invalid filter accessed Windows")
    )
    with pytest.raises(ValueError):
        windows_usb.enumerate_usb("*", "05b8")
