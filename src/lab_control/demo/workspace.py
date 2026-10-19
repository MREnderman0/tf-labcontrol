"""The demonstration uses the production application services with Mock adapters only."""

from pathlib import Path

from lab_control.application.catalog import instrument_category, mode_applicable
from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.instruments.registry import PROFILES, SDK_ADAPTERS

DEMO_MODELS = {
    "signal_generator": ("rigol.dg5504pro", "rs.sma100b"),
    "power_supply": ("siglent.spd4306x", "keithley.2231a"),
    "oscilloscope": ("rs.rtm3004", "pico.6404e"),
    "spectrum_analyzer": ("rigol.dsa815", "rs.fph"),
    "optical_power_meter": ("thorlabs.pm100d", "sim.optical_power_meter"),
    "multimeter": ("keysight.3458a", "keysight.3458a"),
    "network_analyzer": ("rs.znb3004", "rs.znb3004"),
    "phase_noise_analyzer": ("microchip.53100a", "microchip.53100a"),
    "temperature_controller": ("sim.temperature_controller",) * 2,
    "electronic_load": ("sim.electronic_load",) * 2,
    "pump_driver": ("sim.pump_driver",) * 2,
    "optical_spectrometer": ("sim.optical_spectrometer",) * 2,
}


def create_demo_runtime(root: Path | None = None) -> ApplicationRuntime:
    configs = tuple(
        ConnectionConfig(f"mock://workspace/{category}/{index}", adapter, "mock", identity_confirmed=True)
        for category, models in DEMO_MODELS.items()
        for index, adapter in enumerate(models, 1)
        if adapter in PROFILES or adapter in SDK_ADAPTERS or adapter.startswith("sim.")
    )
    runtime = ApplicationRuntime(configs, root=root, hardware=False)
    try:
        ids = [runtime.add(config) for config in configs]
        for device_id in ids:
            runtime.connect(device_id).result()
        records = {r["id"]: r for r in runtime.inventory.all()}
        if not runtime.repository.load_workspace("demo_general_slots_initialized", False):
            for device_id, config in zip(ids, configs, strict=True):
                record = records[device_id]
                if "general" in record.get("slots", {}):
                    continue
                for mode in ("general",):
                    if mode_applicable(record, mode):
                        runtime.bind_slot(
                            device_id,
                            f"{instrument_category(record)}:{config.resource.rsplit('/', 1)[1]}",
                            mode,
                        )
            runtime.repository.save_workspace("demo_general_slots_initialized", True)
        for mode in ("general",):
            key = f"monitor_overview/{mode}"
            if runtime.repository.load_workspace(key) is not None:
                continue
            preferences = {}
            for device_id in ids:
                record = records[device_id]
                if not mode_applicable(record, mode):
                    continue
                slot = record.get("slots", {}).get(mode, "")
                if slot:
                    preferences[f"slot:{slot}"] = {"enabled": True}
            runtime.repository.save_workspace(key, preferences)
        return runtime
    except Exception:
        runtime.close()
        raise
