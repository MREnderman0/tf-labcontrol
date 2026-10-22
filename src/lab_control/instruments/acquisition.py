"""Deprecated parsing imports. Protocol implementations live in model packages."""

from lab_control.instruments.devices.lecroy_waverunner9254.protocol import lecroy_waveform
from lab_control.instruments.devices.rigol_ds1202ze.protocol import scaled_rigol
from lab_control.instruments.devices.rs_znb3004.protocol import complex_trace


def acquire(adapter, request):
    from lab_control.domain.instruments import Unsupported
    from lab_control.instruments.registry import DEVICE_DEFINITIONS

    definition = DEVICE_DEFINITIONS.get(adapter.profile.adapter_id)
    if definition is None or definition.adapter_type is None:
        raise Unsupported("This instrument does not expose a measured data acquisition route")
    # Legacy callers passed a generic ScpiAdapter; dispatch without opening a
    # transport, replacing its state or restoring the removed protocol switch.
    return definition.adapter_type.acquire_measurements(adapter, request)


__all__ = ["scaled_rigol", "lecroy_waveform", "complex_trace", "acquire"]
