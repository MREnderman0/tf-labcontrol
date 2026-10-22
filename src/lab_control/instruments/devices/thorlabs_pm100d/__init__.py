"""Thorlabs PM100D + S154C: pure SDK metadata with explicit connection factories."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation, DeviceModel

from .adapter import SOURCE, PowerMeterAdapter, enumerate_tlpmx


def create_adapter(config):
    from dataclasses import replace

    from lab_control.application.environment import find_sdk
    from lab_control.domain.instruments import Unsupported

    if config.transport not in ("tlpmx", "pm100d"):
        raise Unsupported("Unsupported transport for thorlabs.pm100d")
    if not config.sdk_path:
        config = replace(config, sdk_path=find_sdk(config.transport))
    return PowerMeterAdapter(config)


DEFINITION = DeviceDefinition(
    None,
    None,
    DeviceDocumentation(
        "Thorlabs PM100D + S154C",
        "measurement-analyzers/thorlabs-pm100d-s154c",
        "PM100D USB；S154C 接控制台传感器接口",
        "Thorlabs OPM、匹配的 TLPMX 或旧版 PM100D 64 位 DLL/驱动",
    ),
    Path(__file__).parent,
    deployment_components=("thorlabs-opm",),
    model=DeviceModel(
        "thorlabs.pm100d",
        ("Thorlabs",),
        ("PM100D",),
        "optical_power_meter",
        SOURCE,
        "SDK headers/examples",
        "scalar",
    ),
    factory=create_adapter,
    capability_schema=PowerMeterAdapter.capability_schema,
    discovery_enumerator=enumerate_tlpmx,
    discovery_transports=("tlpmx",),
    discovery_connection_only_transports=("tlpmx", "pm100d"),
    supported_transports=("tlpmx", "pm100d"),
    identity_requires_adapter=True,
)
