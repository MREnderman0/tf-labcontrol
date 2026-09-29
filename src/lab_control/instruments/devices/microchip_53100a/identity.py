"""USB identity normalization follows TimeLab USBDEVICE::serial, not the PnP suffix."""

import re

from lab_control.domain.instruments import DeviceError
from lab_control.instruments.common.usb_descriptors import read_identity


def verify_usb_identity(instance: str, expected_serial: str) -> dict:
    try:
        evidence = read_identity(instance, 0x16D0, 0x05B8)
    except (OSError, ValueError) as exc:
        raise DeviceError("无法读回选定 USB 的身份描述符：" + str(exc), "identity_required") from exc
    strings = evidence["strings"]
    # Official TimeLab 1.780A timelab.cpp, USBDEVICE::serial: stop at ':' or ' '.
    serial = re.split(r"[: ]", strings["serial"], maxsplit=1)[0]
    if (
        strings.get("manufacturer") not in {"Microchip Technology, Inc.", "Miles Design LLC"}
        or strings.get("product") not in {"Microchip 53100A", "PhaseStation 53100A"}
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,127}", serial)
        or serial != expected_serial
    ):
        raise DeviceError("USB 实际身份与选定厂家、型号或铭牌序列号不符", "identity_conflict")
    return {**evidence, "serial": serial, "confidence": "usb_descriptor"}
