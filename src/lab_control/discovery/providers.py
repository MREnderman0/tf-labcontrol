from __future__ import annotations

import hashlib
from dataclasses import replace

from lab_control.discovery.types import DiscoveryCandidate
from lab_control.domain.connections import ConnectionConfig, canonical_resource
from lab_control.transport.visa import VisaRuntime


class VisaDiscoveryProvider:
    name = "visa"

    def __init__(self, runtime: VisaRuntime) -> None:
        self.runtime = runtime

    def enumerate(self) -> tuple[DiscoveryCandidate, ...]:
        # USB instrument manufacturers with documented IEEE 488.2 identity support.
        allowlist = {0x1AB1, 0xF4EC, 0x0699, 0x0AAD, 0x0957, 0x2A8D, 0x05FF}
        candidates = []
        for resource in self.runtime.enumerate():
            parts = resource.split("::")
            safe = False
            if resource.upper().startswith("USB") and resource.upper().endswith("::INSTR"):
                try:
                    safe = int(parts[1], 0) in allowlist
                except (ValueError, IndexError):
                    pass
            candidates.append(
                DiscoveryCandidate(ConnectionConfig(resource), self.name, {"visa_resource": resource}, safe)
            )
        return tuple(candidates)


class SerialDiscoveryProvider:
    name = "serial"

    def enumerate(self) -> tuple[DiscoveryCandidate, ...]:
        from serial.tools.list_ports import comports

        def usb_instance_id(port) -> str:
            # USB-serial bridges usually keep this value when Windows assigns a
            # different COM number after reinsertion.  Do not fall back to the
            # port location: a changed socket is exactly the case we want to
            # support.
            if port.serial_number and port.vid is not None and port.pid is not None:
                return f"VID_{port.vid:04X}&PID_{port.pid:04X}\\{port.serial_number}"
            return ""

        return tuple(
            DiscoveryCandidate(
                ConnectionConfig(
                    port.device,
                    transport="serial",
                    usb_instance_id=usb_instance_id(port),
                ),
                self.name,
                {
                    "description": port.description,
                    "vid": port.vid,
                    "pid": port.pid,
                    "usb_serial": port.serial_number,
                    "usb_instance_id": usb_instance_id(port),
                    "location": port.location,
                },
            )
            for port in comports()
        )


class VendorSdkDiscoveryProvider:
    name = "sdk"

    def __init__(self, configurations: tuple[ConnectionConfig, ...]) -> None:
        self.configurations = configurations
        if configurations:
            self.name = (
                "sdk:"
                + configurations[0].transport
                + ":"
                + hashlib.sha256(configurations[0].sdk_path.encode()).hexdigest()[:8]
            )

    def enumerate(self) -> tuple[DiscoveryCandidate, ...]:
        from lab_control.instruments.registry import DEVICE_DEFINITIONS

        result = []
        seen = set()
        for config in self.configurations:
            if not config.sdk_path:
                from lab_control.application.environment import find_sdk

                config = replace(config, sdk_path=find_sdk(config.transport))
            key = config.transport, config.sdk_path
            if key in seen:
                continue
            seen.add(key)
            definition = DEVICE_DEFINITIONS.get(config.adapter)
            enumerate_resources = (
                definition.discovery_enumerator
                if definition and config.transport in definition.discovery_transports
                else None
            )
            if enumerate_resources:
                for resource in enumerate_resources(config):
                    result.append(
                        DiscoveryCandidate(
                            config
                            if canonical_resource(config.resource) == canonical_resource(resource)
                            else ConnectionConfig(
                                resource,
                                config.adapter,
                                config.transport,
                                timeout=config.timeout,
                                sdk_path=config.sdk_path,
                            ),
                            self.name,
                            {"enumeration": "vendor SDK metadata; no open"},
                        )
                    )
        return tuple(result)


class ConfiguredDiscoveryProvider:
    name = "configured"

    def __init__(self, configurations: tuple[ConnectionConfig, ...]) -> None:
        self.configurations = configurations

    def enumerate(self) -> tuple[DiscoveryCandidate, ...]:
        return tuple(
            DiscoveryCandidate(config, self.name, {"configured": True}) for config in self.configurations
        )
