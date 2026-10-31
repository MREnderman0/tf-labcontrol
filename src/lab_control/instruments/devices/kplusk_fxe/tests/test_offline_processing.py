"""Model-owned offline continuity rules; fixtures contain no real reports."""

import threading
from dataclasses import replace

import numpy as np
import pytest

from lab_control.application.processing_figures import generate_pngs
from lab_control.domain.instruments import DeviceIdentity, Measurement, MeasurementBatch
from lab_control.storage.layout import atomic_json
from lab_control.storage.paths import SaveContext
from lab_control.storage.processing import process_device
from lab_control.storage.processing_results import ResultReader
from lab_control.storage.recording import SegmentWriter
from lab_control.storage.recording_catalog import read_device, read_json


@pytest.mark.parametrize(
    "changed",
    [
        {"report_interval_ms": 2000},
        {"channel_count": 8},
        {"source_report_type": "RT_DOUBLE"},
        {"_session_configuration_version": 1},
        {"_acquisition_gap": {"reason": "reconnected"}},
    ],
)
def test_report_telemetry_does_not_split_frequency_but_settings_do(tmp_path, qapp, changed):
    identity = DeviceIdentity("K+K", "FXE2", "OFFLINE-FIXTURE")
    context = SaveContext(tmp_path, "general", "continuous", identity=identity, adapter_id="kplusk.fxe")
    context.prepare()
    original = []
    writer = SegmentWriter(context.raw_folder)
    for index in range(4):
        config = {
            "device_ms": index * 1000,
            "report_log": f"synthetic report {index}",
            "report_interval_ms": 1000,
            "channel_count": 4,
            "source_report_type": "RT_PHASE",
            "_session_configuration_version": 0,
            **(changed if index == 3 else {}),
        }
        frame = MeasurementBatch(
            identity,
            tuple(
                Measurement("frequency", "Hz", [channel * 1e6 + index], channel) for channel in range(1, 5)
            ),
            config,
            timing={"recording_elapsed_seconds": index},
        )
        frame = replace(frame, timestamp=f"2026-10-31T01:00:0{index}+00:00")
        writer.append(index + 1, frame)
        original.append(frame)
    writer.close()
    atomic_json(
        context.raw_folder / "summary.json",
        {"committed": 4, "binary_done": True, "analysis_state": "complete"},
    )
    cancel = threading.Event()
    folder = process_device(read_device(context.folder), context.folder, cancel)
    reader = ResultReader(folder)
    series = reader.info()["series"]
    data = reader.scalar(tuple(s["id"] for s in series))
    for sample in series:
        x, y = data[sample["id"]]
        np.testing.assert_equal(x, [0, 1, 2, np.nan, 3])
        np.testing.assert_equal(
            y, [sample["channel"] * 1e6 + i if i != -1 else np.nan for i in (0, 1, 2, -1, 3)]
        )
    for index, frame in enumerate(original, 1):
        saved = reader.frame(index)
        assert saved["configuration"] == frame.configuration  # Raw telemetry stays available.
        for expected, actual in zip(frame.measurements, saved["measurements"], strict=True):
            np.testing.assert_array_equal(expected.values, actual["values"])
    generate_pngs(folder, cancel, lambda *_: None)
    manifest = read_json(folder / "png.json")
    assert len(manifest["files"]) == 1
    assert manifest["files"][0]["channels"] == ["CH 1", "CH 2", "CH 3", "CH 4"]
