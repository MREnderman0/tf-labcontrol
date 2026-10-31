"""Random frame access and bounded scalar envelopes from processed results."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import numpy as np

from lab_control.storage.recording_catalog import read_json


class ResultReader:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.metadata = read_json(folder / "result.json", {})
        if (
            not isinstance(self.metadata, dict)
            or self.metadata.get("version") != 1
            or self.metadata.get("state") != "complete"
        ):
            raise ValueError("处理结果未完成或版本不支持")

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect((self.folder / "data.sqlite3").as_uri() + "?mode=ro&immutable=1", uri=True)

    def info(self) -> dict[str, Any]:
        with closing(self.connect()) as db:
            series = [
                {"id": key, **json.loads(document)} for key, document in db.execute("SELECT * FROM series")
            ]
            start, end = db.execute("SELECT min(elapsed),max(elapsed) FROM timeline").fetchone()
        return {**self.metadata, "series": series, "range": (start or 0, end or 0)}

    def frame(self, sequence: int) -> dict[str, Any]:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT frames.document,configs.document,timeline.stamp,timeline.elapsed FROM frames "
                "JOIN configs ON configs.id=frames.config_id JOIN timeline ON timeline.seq=frames.seq "
                "WHERE frames.seq=?",
                (sequence,),
            ).fetchone()
            if row is None:
                raise ValueError("选择的帧不存在")
            samples = []
            for document, data, axis in db.execute(
                "SELECT document,data,axis FROM samples WHERE seq=? ORDER BY idx", (sequence,)
            ):
                sample = json.loads(document)
                sample["values"] = np.frombuffer(data, dtype=sample.pop("dtype")).reshape(sample.pop("shape"))
                dtype, shape = sample.pop("axis_dtype"), sample.pop("axis_shape")
                sample["axis"] = None if axis is None else np.frombuffer(axis, dtype=dtype).reshape(shape)
                samples.append(sample)
            return {
                **json.loads(row[0]),
                **json.loads(row[1]),
                "sequence": sequence,
                "timestamp": row[2],
                "elapsed": row[3],
                "measurements": samples,
            }

    def scalar(
        self, selected: tuple[str, ...], bounds: tuple[float, float] | None = None, points: int = 3000
    ) -> dict[str, tuple[np.ndarray, np.ndarray]]:
        """Stream min/max envelopes, retaining gaps instead of joining missing channels."""
        result = {}
        with closing(self.connect()) as db:
            full = db.execute("SELECT min(elapsed),max(elapsed) FROM timeline").fetchone()
            if full[0] is None:
                return {}
            lo, hi = bounds or full
            # Four extrema/endpoints plus gap markers per pixel bucket. Configuration
            # changes must not turn the bounded display buffer into a full record.
            width = max((hi - lo) / max(1, points // 8), 1e-12)
            for series in selected:
                xs: list[float] = []
                ys: list[float] = []
                bucket = None
                first = last = minimum = maximum = None
                previous = None
                previous_config = None
                segment = 0
                output_segment = None

                def flush():
                    nonlocal output_segment
                    if first is None:
                        return
                    for item in sorted({first, minimum, maximum, last}):
                        if output_segment is not None and output_segment != item[3]:
                            xs.append(float("nan"))
                            ys.append(float("nan"))
                        xs.append(item[1])
                        ys.append(item[2])
                        output_segment = item[3]

                for sequence, elapsed, value, config in db.execute(
                    "SELECT scalar.seq,elapsed,value,config_id FROM scalar JOIN timeline ON timeline.seq=scalar.seq "
                    "WHERE series=? AND elapsed>=? AND elapsed<=? ORDER BY scalar.seq",
                    (series, lo, hi),
                ):
                    current = int((elapsed - lo) / width)
                    gap = previous is not None and (sequence != previous + 1 or config != previous_config)
                    if gap:
                        segment += 1
                    if current != bucket:
                        flush()
                        first = last = minimum = maximum = None
                    item = (sequence, elapsed, value, segment)
                    first = item if first is None else first
                    last = item
                    minimum = item if minimum is None or value < minimum[2] else minimum
                    maximum = item if maximum is None or value > maximum[2] else maximum
                    previous, previous_config, bucket = sequence, config, current
                flush()
                result[series] = (np.asarray(xs), np.asarray(ys))
        return result
