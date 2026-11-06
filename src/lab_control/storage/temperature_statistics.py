"""Exact terminal-window statistics from full scalar data, never plot envelopes."""

import json

from lab_control.domain.rolling_extrema import WINDOW_SECONDS


def temperature_statistics(db) -> dict:
    start, end = db.execute("SELECT min(elapsed),max(elapsed) FROM timeline").fetchone()
    lo = None if end is None else max(start, end - WINDOW_SECONDS)
    result = {"window_seconds": WINDOW_SECONDS, "start_elapsed": lo, "end_elapsed": end, "channels": []}
    for key, document in db.execute("SELECT * FROM series"):
        series = json.loads(document)
        if series["quantity"] != "temperature" or series.get("axis_unit"):
            continue
        count, minimum, maximum = db.execute(
            "SELECT count(*),min(value),max(value) FROM scalar "
            "JOIN timeline ON scalar.seq=timeline.seq "
            "WHERE series=? AND elapsed>=? AND elapsed<=?",
            (key, lo, end),
        ).fetchone()
        result["channels"].append(
            {
                "series_id": key,
                "channel": series["channel"],
                "unit": series["unit"],
                "count": count,
                "minimum": minimum,
                "maximum": maximum,
                "peak_to_peak": None if count == 0 else maximum - minimum,
            }
        )
    result["channels"].sort(key=lambda row: (row["channel"], row["unit"]))
    return result
