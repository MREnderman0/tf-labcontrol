"""Exercise the offline pipeline using this model's deterministic WORD peer."""

import threading
from dataclasses import replace

import numpy as np

from lab_control.domain.instruments import AcquisitionRequest
from lab_control.storage.layout import atomic_json
from lab_control.storage.paths import SaveContext
from lab_control.storage.processing import process_device
from lab_control.storage.processing_results import ResultReader
from lab_control.storage.recording import SegmentWriter
from lab_control.storage.recording_catalog import read_device

from .fake import adapter


def test_word_fixture_to_processed_multichannel_timeline(tmp_path):
    obj, peer = adapter()
    obj.refresh_capabilities()
    obj.prepare_acquisition()
    request = AcquisitionRequest("waveform", (1, 2, 3, 4), consistency="stream", context_id="offline-fixture")
    frames = [
        replace(obj.acquire(request), timing={"recording_elapsed_seconds": index * 0.5}) for index in range(3)
    ]
    context = SaveContext(tmp_path, "general", "continuous", identity=obj.identity, adapter_id="rigol.dho924")
    context.prepare()
    writer = SegmentWriter(context.raw_folder, size=1)
    for index, frame in enumerate(frames, 1):
        writer.append(index, frame)
    writer.close()
    atomic_json(
        context.raw_folder / "summary.json",
        {"committed": 3, "binary_done": True, "analysis_state": "complete"},
    )
    peer.history.clear()
    output = process_device(read_device(context.folder), context.folder, threading.Event())
    reader = ResultReader(output)
    assert reader.info()["kind"] == "frames" and reader.info()["points"] == 12_000
    for index in (3, 1, 2):
        cached = reader.frame(index)
        assert cached["elapsed"] == (index - 1) * 0.5
        for original, actual in zip(frames[index - 1].measurements, cached["measurements"], strict=True):
            np.testing.assert_array_equal(original.values, actual["values"])
            np.testing.assert_array_equal(original.axis, actual["axis"])
            assert actual["unit"] == "V" and actual["axis_unit"] == "s"
            assert actual["metadata"]["trigger_time_source"] == "unavailable"
    assert peer.history == []  # Processing and random reads never call the adapter.
