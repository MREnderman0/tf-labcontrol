"""Accelerated synthetic acquisition for tests/screenshots only; never opens USB."""

import numpy as np

from lab_control.demo.instrument import SimulatedInstrument
from lab_control.domain.instruments import DeviceError, Measurement, MeasurementBatch, Quality
from lab_control.instruments.devices.microchip_53100a.stability import deviations


class PhaseSimulation(SimulatedInstrument):
    recorded_run_kinds = ("timed_phase",)
    started = False
    finished = False

    def __init__(self, config):
        super().__init__(config)
        self.values["1.input_frequency"] = {"automatic": True, "value": None}
        self.values["1.reference_frequency"] = {"automatic": True, "value": None}
        self.values["1.sample_rate"] = {"value": 100}

    def validate_recorded_run(self, request):
        request.validate()
        if request.sample_rate not in (1, 10, 100, 1000) or not request.duration:
            raise DeviceError("Invalid synthetic request")

    def run_acquisition(self, request, stop):
        self.started, self.finished = True, False
        rate, duration = request.sample_rate, request.duration
        n = int(rate * duration) + 1
        phase = np.cumsum(np.random.default_rng(72).normal(size=n)) * 1e-12
        offset = np.geomspace(1, 1e6, 301)
        noise = -145 + 60 / (1 + offset**0.4) + 2 * np.sin(np.log(offset) * 6) / (1 + offset**0.2)
        pn = (
            Measurement(
                "phase_noise", "dBc/Hz", noise, axis=offset, axis_unit="Hz", quality=Quality.SIMULATED
            ),
        )
        count = 0
        try:
            while count < n and not stop.is_set():
                end = min(n, count + int(rate))
                sample = Measurement(
                    "phase_difference",
                    "s",
                    phase[count:end],
                    axis=np.arange(count, end) / rate,
                    axis_unit="s",
                    quality=Quality.SIMULATED,
                )
                count = end
                yield MeasurementBatch(
                    self.identity,
                    (sample,) + pn,
                    {
                        "sample_rate": rate,
                        "requested_duration": duration,
                        "samples": count,
                        "elapsed": (count - 1) / rate,
                        "final": False,
                        "complete": False,
                    },
                    validation="full",
                )
                stop.wait(0.02)
        finally:
            self.finished = True
        yield MeasurementBatch(
            self.identity,
            pn + deviations(phase[:count], rate),
            {
                "sample_rate": rate,
                "requested_duration": duration,
                "samples": count,
                "elapsed": (count - 1) / rate,
                "final": True,
                "complete": count == n,
                "phase_noise_available": True,
                "stability_available": count >= 4,
                "stopped": True,
            },
            validation="full",
        )
