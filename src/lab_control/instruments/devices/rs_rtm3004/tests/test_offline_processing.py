"""REAL32 acquisition, segmented storage and offline frame fidelity for RTM3004."""

import threading
from dataclasses import replace

import numpy as np

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, jsonable
from lab_control.instruments.registry import DEVICE_DEFINITIONS, build_scpi_adapter
from lab_control.storage.layout import atomic_json
from lab_control.storage.paths import SaveContext
from lab_control.storage.processing import process_device
from lab_control.storage.processing_results import ResultReader
from lab_control.storage.recording import SegmentWriter
from lab_control.storage.recording_catalog import read_device
from lab_control.transport.base import ieee_block

from .fake import Peer


def test_real32_frames_preserve_all_channels_coordinates_and_cached_provenance(tmp_path, monkeypatch):
    peer = Peer()
    obj = build_scpi_adapter(ConnectionConfig(peer.resource, "rs.rtm3004", "mock"), peer)
    obj.identity = obj.identify()
    obj.refresh_capabilities()
    obj.prepare_acquisition()
    generation = 0

    def samples(command, limit):
        peer.history.append(command)
        assert limit == 4_000_000 * 4
        channel = int(command[4])
        values = np.sin(np.linspace(0, np.pi * 2, 64) + generation) * channel / 10
        return ieee_block(values.astype("<f4").tobytes())

    monkeypatch.setattr(peer, "query_binary", samples)
    request = AcquisitionRequest("waveform", (1, 2, 3, 4), consistency="stream", context_id="offline-fixture")
    frames = []
    for generation in range(3):
        frames.append(replace(obj.acquire(request), timing={"recording_elapsed_seconds": generation * 0.5}))
    assert [f.validation for f in frames] == ["full", "cached", "cached"]
    context = SaveContext(tmp_path, "general", "continuous", identity=obj.identity, adapter_id="rs.rtm3004")
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
    assert reader.info()["kind"] == "frames" and reader.info()["points"] == 768
    assert DEVICE_DEFINITIONS["rs.rtm3004"].offline_processing.separate_channel_pngs
    for index in (3, 1, 2):
        original = frames[index - 1]
        cached = reader.frame(index)
        assert cached["elapsed"] == (index - 1) * 0.5
        assert cached["settings_timestamps"] == original.settings_timestamps
        assert cached["validation"] == original.validation
        assert cached["configuration"] == original.configuration
        for sample, actual in zip(original.measurements, cached["measurements"], strict=True):
            np.testing.assert_array_equal(sample.values, actual["values"])
            np.testing.assert_array_equal(sample.axis, actual["axis"])
            assert actual["quantity"] == "voltage" and actual["unit"] == "V" and actual["axis_unit"] == "s"
            assert actual["channel"] == sample.channel
            assert actual["metadata"] == jsonable(sample.metadata)
            assert actual["metadata"]["scope_display"]["horizontal_divisions"] == 12
    assert process_device(read_device(context.folder), context.folder, threading.Event()) == output
    assert peer.history == []
