"""Identity from the official 0x7001 version response, never from its IP field."""

from __future__ import annotations

import re

from lab_control.domain.instruments import DeviceError, DeviceIdentity

_VERSION = re.compile(
    r"^(?:7001\s*;?\s*)?VERSION\s+(FXE2?)\s*/\s*#\s*([^\s/]+)"
    r"\s+User\s+\d+\s*/\s*([^/]+)(?:/|$)",
    re.IGNORECASE,
)


def parse_version_identity(raw: str) -> DeviceIdentity:
    match = _VERSION.match(raw.strip().rstrip("\0"))
    if match is None:
        raise DeviceError("K+K version report has no verified model and serial", "identity_required")
    model, serial, firmware = match.groups()
    if serial.casefold() in {"unknown", "none", "0", "-"} or not serial.strip("0"):
        raise DeviceError("K+K version report has no reliable serial", "identity_required")
    return DeviceIdentity("K+K", model.upper(), serial, firmware.strip(), raw=raw, confidence="protocol")
