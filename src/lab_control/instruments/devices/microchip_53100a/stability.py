"""Overlapping Allan and modified Allan deviation from time error in seconds.

Independent implementation of the phase-data formulas in NIST SP 1065. No detrending,
interpolation, gap filling, frequency normalization or copied third-party code.
"""

import numpy as np

from lab_control.domain.instruments import DeviceError, Measurement


def deviations(phase, rate: float) -> tuple[Measurement, ...]:
    x = np.asarray(phase, dtype=np.float64)
    if x.ndim != 1 or not np.isfinite(x).all() or not np.isfinite(rate) or rate <= 0:
        raise DeviceError("Invalid phase samples for stability analysis", "parse_error")
    output: dict[str, list[tuple[float, float, int]]] = {"adev": [], "mdev": []}
    # Octave averaging factors keep long acquisitions bounded and reproducible.
    m = 1
    while len(x) - 2 * m >= 2:
        tau = m / rate
        d = x[2 * m :] - 2 * x[m:-m] + x[: -2 * m]
        output["adev"].append((tau, float(np.sqrt(np.mean(d * d) / (2 * tau * tau))), len(d)))
        if len(d) - m + 1 >= 2:
            summed = np.concatenate(([0.0], np.cumsum(d)))
            moving = (summed[m:] - summed[:-m]) / m
            output["mdev"].append(
                (tau, float(np.sqrt(np.mean(moving * moving) / (2 * tau * tau))), len(moving))
            )
        m *= 2
    result = []
    for name, rows in output.items():
        if rows:
            data = np.asarray(rows)
            if not np.isfinite(data).all():
                raise DeviceError("Stability calculation overflow", "parse_error")
            result.append(
                Measurement(
                    name,
                    "ratio",
                    data[:, 1],
                    axis=data[:, 0],
                    axis_unit="s",
                    metadata={
                        "estimator": "overlapping",
                        "averaging_factors": "octave",
                        "terms": data[:, 2].astype(int).tolist(),
                        "confidence_interval": "not estimated",
                        "detrended": False,
                    },
                )
            )
    return tuple(result)
