"""DSA800 Auto key, Programming Guide 2-138. Never substitutes preset/reset."""

import re
import time

from lab_control.domain.instruments import ApplyResult, DeviceCommand, DeviceError
from lab_control.domain.spectrum import SETTINGS


def auto_setup(adapter, command: DeviceCommand) -> ApplyResult:
    adapter.validate_command(command)
    sent = False
    try:
        adapter.transport.write(":SENSe:POWer:ATUNe")
        sent = True
        deadline = time.monotonic() + max(30.0, adapter.config.timeout)
        while True:
            reply = adapter.transport.query("*OPC?").strip()
            if reply == b"1":
                break
            if reply != b"0":
                raise DeviceError("Invalid Auto completion response", "parse_error")
            if time.monotonic() >= deadline:
                raise DeviceError("Auto Tune completion timed out", "timeout")
            time.sleep(0.1)
        error = adapter.transport.query("SYST:ERR?").decode("ascii").strip()
        if not re.match(r"^[+]?0(?:\s|,|$)", error):
            raise DeviceError(error, "auto_setup_failed")
        values = {name: adapter.read(name) for name in SETTINGS}
        return ApplyResult("success", "auto_setup", {}, values, 1)
    except (DeviceError, UnicodeError) as exc:
        return ApplyResult(
            "partial" if sent else "uncertain",
            "auto_setup",
            {},
            completed_steps=int(sent),
            error=f"Auto 结果未确认：{exc}",
        )
