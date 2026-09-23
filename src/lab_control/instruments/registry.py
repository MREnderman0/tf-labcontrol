from __future__ import annotations

import re
from urllib.parse import urlparse

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import (
    CapabilitySet,
    DeviceError,
    DeviceIdentity,
    InstrumentAdapter,
    Unsupported,
)
from lab_control.instruments.analyzers import PROFILES as ANALYZERS
from lab_control.instruments.base import Profile, ScpiAdapter
from lab_control.instruments.oscilloscopes import PROFILES as SCOPES
from lab_control.instruments.power_supplies import PROFILES as POWERS
from lab_control.instruments.power_supplies import PowerAdapter
from lab_control.instruments.signal_sources import PROFILES as SIGNALS
from lab_control.instruments.signal_sources import SignalGeneratorAdapter
from lab_control.transport.base import MockTransport, Transport
from lab_control.transport.streams import SerialTransport, TcpSocketTransport, VicpTransport
from lab_control.transport.visa import VisaRuntime, VisaTransport

PROFILES: dict[str, Profile] = {
    profile.adapter_id: profile for profile in (*SIGNALS, *POWERS, *SCOPES, *ANALYZERS)
}
SDK_ADAPTERS = {
    "thorlabs.pm100d": "optical_power_meter",
    "pico.6404e": "oscilloscope",
    "microchip.53100a": "phase_noise_analyzer",
}
PENDING = {
    "PSW160": "Manufacturer and complete suffix required",
    "DS1202Z": "Confirm exact suffix; DS1202Z-E adapter is deliberately not an alias",
    "GOCA-8-NK2603212": "Manufacturer/protocol required",
    "multi-channel optical power meter": "Model and protocol required",
    "temperature_controller": "Model and protocol required",
    "electronic_load": "Model and protocol required",
    "pump_driver": "Model, protocol and interlocks required",
    "optical_spectrometer": "Model and protocol required",
}


def describe(config: ConnectionConfig) -> CapabilitySet:
    """Inspect declared capabilities without loading a driver or opening an instrument."""
    if config.transport == "mock" and config.adapter.startswith("sim."):
        from lab_control.demo.instrument import SimulatedInstrument

        return SimulatedInstrument(config).capabilities()
    if config.adapter in PROFILES:
        return ScpiAdapter(MockTransport([]), PROFILES[config.adapter], config).capabilities()
    from lab_control.instruments.optical import PowerMeterAdapter
    from lab_control.instruments.phase_noise import PhaseNoiseAdapter
    from lab_control.instruments.pico import PicoAdapter

    schemas = {
        "thorlabs.pm100d": PowerMeterAdapter.capability_schema,
        "pico.6404e": PicoAdapter.capability_schema,
        "microchip.53100a": PhaseNoiseAdapter.capability_schema,
    }
    if config.adapter not in schemas:
        raise Unsupported("No capability schema for this model")
    return schemas[config.adapter](config)


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
    if config.transport == "visa":
        return VisaTransport(
            runtime,
            config.resource,
            config.timeout,
            config.terminator,
            config.baud_rate,
            line_framing=config.adapter == "keysight.3458a",
        )
    if config.transport == "serial":
        return SerialTransport(config.resource, config.baud_rate, config.timeout, config.terminator.encode())
    endpoint = urlparse(config.resource)
    if not endpoint.hostname or endpoint.path not in ("", "/"):
        raise DeviceError("Use tcp://host:port or vicp://host:port", "invalid_parameter")
    if config.transport == "tcp":
        return TcpSocketTransport(
            endpoint.hostname, endpoint.port or 5025, config.timeout, config.terminator.encode()
        )
    if config.transport == "vicp":
        return VicpTransport(endpoint.hostname, endpoint.port or 1861, config.timeout)
    raise Unsupported("No byte transport for this configuration")


def create_adapter(config: ConnectionConfig, runtime: VisaRuntime) -> InstrumentAdapter:
    if config.transport == "mock":
        from lab_control.demo.instrument import SimulatedInstrument

        return SimulatedInstrument(config)
    if config.adapter == "thorlabs.pm100d" and config.transport in {"tlpmx", "pm100d"}:
        from lab_control.instruments.optical import PowerMeterAdapter

        return PowerMeterAdapter(config)
    if config.adapter == "pico.6404e" and config.transport == "pico":
        from lab_control.instruments.pico import PicoAdapter

        return PicoAdapter(config)
    if config.adapter == "microchip.53100a" and config.transport == "tserve":
        from lab_control.instruments.phase_noise import PhaseNoiseAdapter

        return PhaseNoiseAdapter(config)
    if config.adapter not in PROFILES:
        raise Unsupported("Select an implemented, exact model adapter before connecting")
    profile = PROFILES[config.adapter]
    transport = transport_for(config, runtime)
    try:
        transport.open()
        adapter_class = (
            SignalGeneratorAdapter
            if profile.category == "signal_generator"
            else (PowerAdapter if profile.category == "power_supply" else ScpiAdapter)
        )
        return adapter_class(transport, profile, config)
    except Exception:
        transport.close()
        raise
