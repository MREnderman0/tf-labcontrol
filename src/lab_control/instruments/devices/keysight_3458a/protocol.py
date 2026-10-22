from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

import numpy as np

from lab_control.domain.instruments import (
    AcquisitionRequest,
    DeviceError,
    Measurement,
    Quality,
    Unsupported,
)
from lab_control.transport.base import numeric_values

if TYPE_CHECKING:
    from lab_control.instruments.base import ScpiAdapter


def acquire(adapter: ScpiAdapter, request: AcquisitionRequest) -> tuple[Measurement, ...]:
    transport = adapter.transport
    if request.kind != "scalar":
        raise Unsupported("Use acquisition kind=scalar for this model")
    if not request.confirmed:
        raise DeviceError(
            "3458A capture needs confirmation: RMEM stops memory recording; initiate reconfigures triggering",
            "confirmation_required",
        )
    result: list[Measurement] = []
    transport.write("OFORMAT ASCII")
    function = adapter.read("function_status")
    if request.initiate:
        transport.write("TARM HOLD")
        transport.write("MEM OFF")
        transport.write(f"NRDGS {request.points},AUTO")
        transport.write("TRIG AUTO")
        transport.write("TARM SGL")
    else:
        transport.write(f"RMEM 1,{request.points}")
    parts, size = ([], 0)
    deadline = time.monotonic() + adapter.config.timeout
    while size < request.points:
        if time.monotonic() > deadline:
            raise DeviceError("3458A readings timed out", "timeout")
        part = numeric_values(transport.read())
        if not part.size or size + part.size > request.points:
            raise DeviceError("3458A reading count mismatch", "parse_error")
        parts.append(part)
        size += part.size
    values = np.concatenate(parts)
    units = {
        "DCV": "V",
        "ACV": "V",
        "ACDCV": "V",
        "DCI": "A",
        "ACI": "A",
        "ACDCI": "A",
        "OHM": "ohm",
        "OHMF": "ohm",
        "FREQ": "Hz",
        "PER": "s",
    }
    field = re.sub("^FUNC\\s+", "", str(function).upper()).split(",")[0].strip()
    codes = {
        1: "DCV",
        2: "ACV",
        3: "ACDCV",
        4: "OHM",
        5: "OHMF",
        6: "DCI",
        7: "ACI",
        8: "ACDCI",
        9: "FREQ",
        10: "PER",
        11: "DCV",
        12: "DCV",
        13: "DCV",
        14: "DCV",
    }
    try:
        field = codes.get(int(float(field)), field)
    except ValueError:
        pass
    unit = units.get(field, "")
    quality = Quality.OVERLOAD if np.any(np.abs(values) >= 1e37) else Quality.GOOD
    result.append(Measurement("reading", unit, values, quality=quality, metadata={"function_code": function}))
    return tuple(result)
