"""Terminal-window metrics retain complete precision and old temperature values."""

import threading
from dataclasses import replace

import pytest

from lab_control.application.processing_figures import generate_pngs
from lab_control.domain.instruments import Measurement
from lab_control.storage.layout import atomic_json
from lab_control.storage.processing_results import ResultReader
from tests.unit.test_data_processing import capture, frame, processed


def temperature_frame(at, values):
    return replace(
        frame(),
        timing={"recording_elapsed_seconds": at},
        measurements=tuple(Measurement("temperature", "°C", [value], ch) for ch, value in values.items()),
    )


def test_final_window_uses_record_end_for_every_channel_and_full_data(tmp_path):
    frames = [
        temperature_frame(at, values)
        for at, values in (
            (0, {401: -99, 417: 100}),
            (200, {401: 1, 417: 2}),
            (201, {401: 3}),
            (1000, {401: -2}),
            (2000, {401: 4}),
        )
    ]
    reader = ResultReader(processed(capture(tmp_path, frames, adapter="rigol.m300")))
    stats = reader.info()["temperature_statistics"]
    assert (stats["start_elapsed"], stats["end_elapsed"]) == (200, 2000)
    one, two = stats["channels"]
    assert (one["count"], one["minimum"], one["maximum"], one["peak_to_peak"]) == (4, -2, 4, 6)
    assert (two["count"], two["peak_to_peak"]) == (1, 0)
    reader.scalar((one["series_id"],), (1999, 2000), points=1)
    assert reader.info()["temperature_statistics"] == stats
    assert reader.frame(1)["measurements"][0]["values"][0] == -99  # Historical temperatures stay unchanged.


@pytest.mark.parametrize("end", [10, 1800, 1800.001])
def test_short_record_inclusive_boundary_and_empty_channel(tmp_path, end):
    frames = [temperature_frame(0, {401: -10, 417: 2}), temperature_frame(end, {401: 20})]
    output = processed(capture(tmp_path, frames, adapter="rigol.m300"))
    reader = ResultReader(output)
    stats = reader.info()["temperature_statistics"]
    a, b = stats["channels"]
    assert a["peak_to_peak"] == (30 if end <= 1800 else 0)
    assert b["peak_to_peak"] == (0 if end <= 1800 else None)
    # Older caches compute the same metrics read-only, without recalculating temperature.
    metadata = dict(reader.metadata)
    metadata.pop("temperature_statistics")
    metadata["version"] = 2
    atomic_json(output / "result.json", metadata)
    assert ResultReader(output).info()["temperature_statistics"] == stats


def test_automatic_png_summary_contains_complete_statistic_manifest(qapp, tmp_path):
    frames = [temperature_frame(0, {401: -1, 417: 20}), temperature_frame(10, {401: 2, 417: 20.0002})]
    output = processed(capture(tmp_path, frames, adapter="rigol.m300"))
    generate_pngs(output, threading.Event(), lambda *args: None)
    import json

    manifest = json.loads((output / "png.json").read_text(encoding="utf-8"))
    assert len(manifest["files"]) == 2  # Whole-record curve plus table.
    table = next(row for row in manifest["files"] if "statistics" in row)
    assert table["statistics"] == ResultReader(output).info()["temperature_statistics"]
    assert (output / manifest["directory"] / table["path"]).is_file()
    generate_pngs(output, threading.Event(), lambda *args: None)
    assert json.loads((output / "png.json").read_text(encoding="utf-8")) == manifest
