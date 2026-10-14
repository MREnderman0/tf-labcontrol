"""The single registration list for independent model packages.

Remove a line before deleting its package. No directory scanning, driver
loading, or resource probing is performed while assembling definitions.
"""

from __future__ import annotations

from importlib import import_module

from lab_control.instruments.devices.definition import DeviceDefinition

DEVICE_MODULES = (
    "lab_control.instruments.devices.rigol_dg5504pro",
    "lab_control.instruments.devices.tek_afg31252",
    "lab_control.instruments.devices.siglent_sdg2082x",
    "lab_control.instruments.devices.rs_sma100b",
    "lab_control.instruments.devices.gwinstek_gpd4303s",
    "lab_control.instruments.devices.rigol_dsa815",
    "lab_control.instruments.devices.lecroy_waverunner9404m",
    "lab_control.instruments.devices.rigol_dho924",
    "lab_control.instruments.devices.rs_fph",
    "lab_control.instruments.devices.ceyear_6362d",
    "lab_control.instruments.devices.microchip_53100a",
    "lab_control.instruments.devices.joinwit_jw8105",
    "lab_control.instruments.devices.coremorrow_xmt",
    "lab_control.instruments.devices.rigol_m300",
    "lab_control.instruments.devices.kplusk_fxe",
)


def load_definitions(modules: tuple[str, ...] = DEVICE_MODULES) -> dict[str, DeviceDefinition]:
    definitions = {}
    for module in modules:
        definition = import_module(module).DEFINITION
        if not isinstance(definition, DeviceDefinition):
            raise TypeError(f"{module} must export a DeviceDefinition as DEFINITION")
        if definition.adapter_id in definitions:
            raise ValueError(f"Duplicate adapter ID: {definition.adapter_id}")
        definitions[definition.adapter_id] = definition
    return definitions
