import struct

import pytest

from lab_control.instruments.common import usb_descriptors


def usb_fixture(*, driver="driver-key", vendor=0x16D0, serial="TEST123:12345.6", malformed=False):
    calls = []

    def ioctl(code, payload, capacity):
        calls.append((code, payload))
        if code == 0x220420:
            value = (driver + "\0").encode("utf-16-le")
            return struct.pack("<II", 2, 8 + len(value)) + value
        if code == 0x220448:
            return struct.pack("<I", 2) + struct.pack(
                "<BBHBBBBHHHBBBB", 18, 1, 0x200, 0, 0, 0, 64, vendor, 0x05B8, 0x100, 1, 2, 3, 1
            )
        assert code == 0x220410 and capacity == 267
        port, direction, request, value, language, length = struct.unpack("<IBBHHH", payload)
        assert (port, direction, request, length) == (2, 0x80, 6, 255)
        assert value >> 8 == 3 and language in (0, 0x409)
        strings = {
            0: b"\x09\x04",
            1: "Manufacturer".encode("utf-16-le"),
            2: "Product".encode("utf-16-le"),
            3: serial.encode("utf-16-le"),
        }
        raw = strings[value & 255]
        return payload + bytes((2 + len(raw), 3)) + (raw[:-1] if malformed else raw)

    return ioctl, calls


def test_descriptor_requests_are_selected_port_read_only_and_preserve_raw_serial():
    ioctl, calls = usb_fixture()
    result = usb_descriptors._read_strings(ioctl, 2, "driver-key", 0x16D0, 0x05B8)
    assert result["serial"] == "TEST123:12345.6"
    assert [code for code, _ in calls] == [0x220420, 0x220448, *([0x220410] * 4)]


@pytest.mark.parametrize("changes,count", [({"driver": "different"}, 1), ({"vendor": 0x1234}, 2)])
def test_wrong_port_is_rejected_before_device_descriptor_request(changes, count):
    ioctl, calls = usb_fixture(**changes)
    with pytest.raises(OSError):
        usb_descriptors._read_strings(ioctl, 2, "driver-key", 0x16D0, 0x05B8)
    assert len(calls) == count


@pytest.mark.parametrize("changes", [{"malformed": True}, {"serial": "TEST\0other"}, {"serial": ""}])
def test_incomplete_or_invalid_strings_fail_closed(changes):
    ioctl, _ = usb_fixture(**changes)
    with pytest.raises(OSError):
        usb_descriptors._read_strings(ioctl, 2, "driver-key", 0x16D0, 0x05B8)


def test_invalid_instance_never_opens_hub(monkeypatch):
    monkeypatch.setattr(usb_descriptors.sys, "platform", "win32")
    monkeypatch.setattr(usb_descriptors, "_read_selected", lambda *_: pytest.fail("Opened a hub"))
    with pytest.raises(ValueError):
        usb_descriptors.read_identity("USB\\OTHER\\device", 0x16D0, 0x05B8)
