"""Transport selection for explicitly authorized hardware acceptance runs."""

from __future__ import annotations

import re

from lab_control.application.config import ConnectionConfig


def interface_label(config: ConnectionConfig) -> str:
    return (
        "LAN"
        if config.resource.upper().startswith("TCPIP")
        else "USB"
        if config.resource.upper().startswith("USB")
        else "unknown"
    )


def visa_interface(config: ConnectionConfig) -> str:
    """Require an exact, serial-bound USB or LAN instrument; never guess a host."""
    resource = config.resource.upper()
    if config.transport != "visa" or not re.fullmatch(r"[A-Za-z0-9_./-]+", config.expected_serial):
        raise ValueError("An exact VISA configuration with expected serial is required")
    if any(c in resource for c in "*? \t"):
        raise ValueError("Wildcard resources are not accepted")
    if resource.startswith("USB") and resource.endswith("::INSTR"):
        return "USB"
    if re.fullmatch(r"TCPIP\d*::[^:*?\s]+(?:::[^:*?\s]+)?::INSTR", resource):
        return "LAN"
    raise ValueError("Only explicitly configured USB or TCPIP INSTR resources are accepted")


def enumerate_configured(runtime, config: ConnectionConfig) -> tuple[str, ...]:
    """Enumeration is separate from identity verification; LAN may require explicit addressing."""
    if visa_interface(config) == "USB":
        resources = tuple(runtime.manager.list_resources("USB?*::INSTR"))
        if config.resource.upper() not in {item.upper() for item in resources}:
            raise RuntimeError("Configured USB asset not enumerated")
        return resources
    return tuple(runtime.discover_lan(5))
