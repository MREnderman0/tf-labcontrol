import hashlib

from lab_control.deployment.file_components import inspect_files_usb
from lab_control.instruments.common import windows_usb


def test_file_component_check_does_not_promote_driver_failure(tmp_path, monkeypatch):
    exe = tmp_path / "service.exe"
    exe.write_bytes(b"vendor fixture")
    detection = {
        "executables": [str(exe)],
        "files": [{"name": exe.name, "sha256": hashlib.sha256(exe.read_bytes()).hexdigest()}],
        "usb_vendor": "16D0",
        "usb_product": "05B8",
    }
    monkeypatch.setattr(windows_usb, "enumerate_usb", lambda *_: ({"ConfigManagerErrorCode": 28},))
    assert inspect_files_usb(detection)[0] == "missing"
    monkeypatch.setattr(windows_usb, "enumerate_usb", lambda *_: ({"ConfigManagerErrorCode": 0},))
    state, reason = inspect_files_usb(detection)
    assert state == "ok" and "硬件身份仍须核验" in reason
    exe.write_bytes(b"different vendor version")
    assert inspect_files_usb(detection)[0] == "unknown"
