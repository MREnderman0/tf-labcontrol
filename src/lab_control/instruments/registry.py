from __future__ import annotations

import re
from urllib.parse import urlparse

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import (
    CapabilitySet,
    DeviceError,
    DeviceIdentity,
    InstrumentAdapter,
    Unsupported,
)
from lab_control.instruments.base import Profile, ScpiAdapter
from lab_control.instruments.devices.catalog import load_definitions
from lab_control.instruments.devices.definition import DeviceDefinition
from lab_control.transport.base import MockTransport, Transport
from lab_control.transport.streams import ScpiSocketTransport, SerialTransport, VicpTransport
from lab_control.transport.visa import VisaRuntime, VisaTransport

DEVICE_DEFINITIONS = load_definitions()


def collect_profiles(profiles: tuple[Profile, ...]) -> dict[str, Profile]:
    result = {}
    for profile in profiles:
        if profile.adapter_id in result:
            raise ValueError(f"Duplicate adapter ID: {profile.adapter_id}")
        result[profile.adapter_id] = profile
    return result


PROFILES = collect_profiles(
    (*(definition.profile for definition in DEVICE_DEFINITIONS.values() if definition.profile is not None),)
)
SDK_ADAPTERS = {d.adapter_id: d.category for d in DEVICE_DEFINITIONS.values() if d.profile is None}
PENDING = {
    "PSW160": "Manufacturer and complete suffix required",
    "DS1202Z": "Confirm exact suffix; DS1202Z-E adapter is deliberately not an alias",
    "GOCA-8-NK2603212": "Manufacturer/protocol required",
    "multi-channel optical power meter": "Model and protocol required",
    "temperature_controller": "Model and protocol required",
    "electronic_load": "Model and protocol required",
    "pump_driver": "Model, protocol and interlocks required",
    "other optical_spectrometers": "Exact model and protocol required; Ceyear 6362D has an independent module",
}


def describe(config: ConnectionConfig, channel: int | None = None) -> CapabilitySet:
    """Inspect declared capabilities without loading a driver or opening an instrument."""
    if config.transport == "mock" and config.adapter.startswith("sim."):
        from lab_control.demo.instrument import SimulatedInstrument

        return SimulatedInstrument(config).capabilities()
    definition = DEVICE_DEFINITIONS.get(config.adapter)
    if definition is None:
        raise Unsupported(f"设备适配器不可用或已移除：{config.adapter}")
    return describe_definition(definition, config, channel)


def describe_definition(
    definition: DeviceDefinition, config: ConnectionConfig, channel: int | None = None
) -> CapabilitySet:
    """Inspect an explicit definition, including isolated documentation registries."""
    if definition.capability_schema:
        caps = definition.capability_schema(config)
        if channel is not None and channel not in caps.channels:
            raise Unsupported(f"Invalid channel {channel}")
        return caps
    if definition.profile is None or definition.adapter_type is None:
        raise Unsupported(f"No pure capability schema for {definition.adapter_id}")
    adapter = definition.adapter_type(MockTransport([]), definition.profile, config)
    return adapter.capabilities() if channel is None else adapter.capabilities_for_channel(channel)


def match(identity: DeviceIdentity) -> str:
    def normalize(text: str) -> str:
        return re.sub(r"[\s_-]", "", text).upper()

    for profile in PROFILES.values():
        if normalize(identity.model) in {normalize(value) for value in profile.models} and any(
            normalize(maker) in normalize(identity.manufacturer) for maker in profile.manufacturer
        ):
            return profile.adapter_id
    return ""


def transport_for(config: ConnectionConfig, runtime: VisaRuntime) -> Transport:
    definition = DEVICE_DEFINITIONS.get(config.adapter)
    if definition and definition.transport_factory:
        return definition.transport_factory(config, runtime)
    return default_transport_for(config, runtime)


def default_transport_for(config: ConnectionConfig, runtime: VisaRuntime) -> Transport:
    """Build a standard transport without any model protocol decisions."""
    if config.transport == "visa":
        return VisaTransport(
            runtime,
            config.resource,
            config.timeout,
            config.terminator,
            config.baud_rate,
        )
    if config.transport == "serial":
        return SerialTransport(config.resource, config.baud_rate, config.timeout, config.terminator.encode())
    endpoint = urlparse(config.resource)
    if config.transport == "tcp":
        if endpoint.scheme == "tcp" and endpoint.hostname and endpoint.path in ("", "/"):
            host, port = endpoint.hostname, endpoint.port or 5025
        else:
            match = re.fullmatch(r"TCPIP\d*::([^:]+)::(\d+)::SOCKET", config.resource, re.I)
            if not match:
                raise DeviceError("Use tcp://host:port or TCPIP::host::port::SOCKET", "invalid_parameter")
            host, port = match[1], int(match[2])
        return ScpiSocketTransport(
            host,
            port,
            config.timeout,
            config.terminator.encode(),
            config.source_address,
        )
    if config.transport == "vicp":
        if not endpoint.hostname or endpoint.path not in ("", "/"):
            raise DeviceError("Use vicp://host:port", "invalid_parameter")
        return VicpTransport(endpoint.hostname, endpoint.port or 1861, config.timeout)
    raise Unsupported("No byte transport for this configuration")


def normalize_discovery_config(
    config: ConnectionConfig, source_addresses: tuple[str, ...]
) -> ConnectionConfig:
    """Apply an optional model-owned connection policy before discovery I/O."""
    definition = DEVICE_DEFINITIONS.get(config.adapter)
    if definition and definition.discovery_config:
        return definition.discovery_config(config, source_addresses)
    return config


def build_scpi_adapter(config: ConnectionConfig, transport: Transport) -> ScpiAdapter:
    """Use the same model factory for real sessions and injected protocol transports.

    The caller owns opening/closing the transport. This function performs no I/O.
    """
    definition = DEVICE_DEFINITIONS.get(config.adapter)
    if definition is None or definition.profile is None or definition.adapter_type is None:
        raise Unsupported(f"设备适配器不可用或已移除：{config.adapter}")
    return definition.adapter_type(transport, definition.profile, config)


def create_adapter(config: ConnectionConfig, runtime: VisaRuntime) -> InstrumentAdapter:
    if config.transport == "mock":
        from lab_control.demo.instrument import SimulatedInstrument

        if config.adapter not in DEVICE_DEFINITIONS and not config.adapter.startswith("sim."):
            raise Unsupported(f"设备适配器不可用或已移除：{config.adapter}")
        return SimulatedInstrument(config)
    definition = DEVICE_DEFINITIONS.get(config.adapter)
    if definition is None:
        raise Unsupported(f"设备适配器不可用或已移除：{config.adapter}")
    if definition.supported_transports and config.transport not in definition.supported_transports:
        raise Unsupported(f"Unsupported transport for {config.adapter}: {config.transport}")
    if definition.factory:
        return definition.factory(config)
    if config.adapter not in PROFILES:
        raise Unsupported(f"设备适配器不可用或已移除：{config.adapter}")
    transport = transport_for(config, runtime)
    try:
        transport.open()
        return build_scpi_adapter(config, transport)
    except Exception:
        transport.close()
        raise
