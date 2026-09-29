"""Backward-compatible imports; all 53100A implementation lives in its model package."""

from lab_control.instruments.devices.microchip_53100a.adapter import Microchip53100AAdapter, create
from lab_control.instruments.devices.microchip_53100a.profile import SOURCE, profile
from lab_control.instruments.devices.microchip_53100a.transport import TserveTransport

__all__ = ["PhaseNoiseAdapter", "SOURCE", "TserveTransport"]


class PhaseNoiseAdapter(Microchip53100AAdapter):
    def __init__(self, config, transport=None):
        if transport is None:
            self.__dict__.update(create(config).__dict__)
        else:
            super().__init__(transport, profile(), config)
