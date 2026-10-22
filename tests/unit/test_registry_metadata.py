"""Pure registry contracts shared by UI, documentation and installed packages."""

import ctypes

import pytest

from lab_control.domain.connections import ConnectionConfig
from lab_control.instruments import registry
from lab_control.instruments.base import ScpiAdapter
from lab_control.instruments.devices.catalog import load_definitions
from lab_control.transport.base import MockTransport


@pytest.mark.parametrize(
    "definition",
    [d for d in registry.DEVICE_DEFINITIONS.values() if d.profile is not None and d.profile.acquisition],
    ids=lambda definition: definition.adapter_id,
)
def test_declared_scpi_acquisition_has_model_dispatch(definition):
    adapter_type = definition.adapter_type
    assert adapter_type.acquire is not ScpiAdapter.acquire or (
        adapter_type.acquire_measurements is not ScpiAdapter.acquire_measurements
    ), f"{definition.adapter_id} declares acquisition but only inherits the unsupported fallback"


@pytest.mark.parametrize("options", [(), ("SMAB-K720",), ("SMAB-K720", "SMAB-K24")])
def test_model_option_gate_is_identical_for_whole_device_and_channel(options):
    config = ConnectionConfig("metadata://test", "rs.sma100b", options=options)
    whole = registry.describe(config)
    channel = registry.describe(config, 1)
    for name in ("am_enabled", "fm_enabled", "pm_enabled", "am_shape", "pulse_enabled"):
        assert next(c for c in whole.operations if c.name == name) == next(
            c for c in channel.operations if c.name == name
        )
    shape = next(c for c in whole.operations if c.name == "am_shape")
    assert shape.available == ("SMAB-K720" in options)
    assert shape.writable == ("SMAB-K24" in options)


def test_transport_dependent_metadata_keeps_model_query_restriction():
    config = ConnectionConfig("tcp://192.0.2.1:5555", "rigol.dsa815", "tcp")
    caps = registry.describe(config)
    option = next(c for c in caps.operations if c.name == "options")
    assert not option.readable
    assert option == next(c for c in registry.describe(config, 1).operations if c.name == "options")


def test_catalog_is_the_only_registration_source_and_sdk_metadata_never_loads_drivers(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Metadata inspection loaded a driver")

    monkeypatch.setattr(ctypes, "CDLL", forbidden)
    monkeypatch.setattr(ctypes, "WinDLL", forbidden, raising=False)
    definitions = load_definitions()
    assert set(definitions) == set(registry.PROFILES) | set(registry.SDK_ADAPTERS)
    assert not set(registry.PROFILES) & set(registry.SDK_ADAPTERS)
    for adapter_id in registry.SDK_ADAPTERS:
        definition = definitions[adapter_id]
        assert definition.profile is None and definition.adapter_type is None
        assert definition.model is not None
        assert registry.describe(ConnectionConfig("metadata://test", adapter_id)).operations


def test_legacy_sdk_and_profile_imports_resolve_to_model_implementations():
    from lab_control.instruments.optical import PowerMeterAdapter
    from lab_control.instruments.pico import PicoAdapter
    from lab_control.instruments.power_supplies import PROFILES as old_power_profiles
    from lab_control.instruments.signal_sources import SignalGeneratorAdapter

    assert ".devices.thorlabs_pm100d." in PowerMeterAdapter.__module__
    assert ".devices.pico_6404e." in PicoAdapter.__module__
    assert ".devices.siglent_sdg6032x." in SignalGeneratorAdapter.__module__
    for profile in old_power_profiles:
        wire = MockTransport([])
        adapter = registry.build_scpi_adapter(ConnectionConfig("metadata://test", profile.adapter_id), wire)
        assert ".devices." in type(adapter).__module__ and not wire.history
