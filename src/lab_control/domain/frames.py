"""Shared publication and recording contracts, independent of instrument protocols."""

from __future__ import annotations

import sys
from copy import deepcopy
from dataclasses import fields, is_dataclass, replace
from typing import Any

import numpy as np

from lab_control.domain.instruments import DeviceError, MeasurementBatch, Quality, ValidationError


class FrozenDict(dict):
    """JSON-compatible immutable metadata; construction and deepcopy stay safe."""

    def _readonly(self, *args, **kwargs):
        raise TypeError("Published frame metadata is immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _readonly

    def __deepcopy__(self, memo):
        return self


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return FrozenDict((key, _freeze(item)) for key, item in value.items())
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, np.ndarray):
        return np.frombuffer(value.tobytes(), dtype=value.dtype).reshape(value.shape)
    return deepcopy(value)


def immutable_batch(batch: MeasurementBatch) -> MeasurementBatch:
    """Detach adapter-owned storage before publishing to concurrent consumers."""

    def array(value: Any) -> np.ndarray:
        data = np.asarray(value)
        if data.dtype.hasobject:
            raise ValidationError("Object arrays cannot be published")
        # A bytes owner also prevents a consumer from re-enabling WRITEABLE.
        return np.frombuffer(data.tobytes(), dtype=data.dtype).reshape(data.shape)

    return replace(
        batch,
        measurements=tuple(
            replace(
                m,
                values=array(m.values),
                axis=None if m.axis is None else array(m.axis),
                metadata=_freeze(m.metadata),
            )
            for m in batch.measurements
        ),
        configuration=_freeze(batch.configuration),
        settings_timestamps=_freeze(batch.settings_timestamps),
        performance=_freeze(batch.performance),
        timing=_freeze(batch.timing),
    )


def validate_recorded_batch(batch: MeasurementBatch) -> None:
    if batch.validation == "live":
        raise DeviceError("完整采集后才能保存，不能补用较晚的参数快照", "incomplete_snapshot")
    software_keys = {
        "_session_configuration_version",
        "monitor_record_session",
        "monitor_request",
        "monitor_refresh_interval",
        "monitor_save_interval",
        "monitor_cadence",
    }
    if batch.validation == "cached" and (
        not batch.performance.get("acquisition_context_id")
        or not batch.settings_timestamps
        or any(not stamp for stamp in batch.settings_timestamps.values())
        or not (set(batch.configuration) - software_keys) <= batch.settings_timestamps.keys()
    ):
        raise DeviceError("缺少本次采集的参数快照来源，未保存数据", "incomplete_snapshot")
    if not batch.measurements or any(
        m.quality not in {Quality.GOOD, Quality.SIMULATED} for m in batch.measurements
    ):
        raise DeviceError("采集失败，未保存数据", "acquisition_failed")


def validate_displayed_batch(batch: MeasurementBatch) -> None:
    """Validate samples for snapshot export, without claiming full configuration checks."""
    if not batch.measurements:
        raise DeviceError("尚无有效显示帧", "acquisition_failed")
    for measurement in batch.measurements:
        values = np.asarray(measurement.values)
        axis = None if measurement.axis is None else np.asarray(measurement.axis)
        if (
            measurement.quality not in {Quality.GOOD, Quality.SIMULATED}
            or not values.size
            or not np.all(np.isfinite(values))
            or (axis is not None and (axis.size != values.size or not np.all(np.isfinite(axis))))
        ):
            raise DeviceError("显示帧数据无效，未保存", "acquisition_failed")


def batch_bytes(batch: MeasurementBatch) -> int:
    seen: set[int] = set()

    def size(value: Any) -> int:
        if id(value) in seen:
            return 0
        seen.add(id(value))
        if isinstance(value, np.ndarray):
            return value.nbytes + sys.getsizeof(value) + 64
        overhead = sys.getsizeof(value)
        if is_dataclass(value) and not isinstance(value, type):
            return overhead + sum(size(getattr(value, f.name)) for f in fields(value))
        if isinstance(value, dict):
            return overhead + sum(size(key) + size(item) for key, item in value.items())
        if isinstance(value, (list, tuple)):
            return overhead + sum(size(item) for item in value)
        return overhead

    return size(batch)
