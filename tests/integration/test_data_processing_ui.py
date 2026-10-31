"""History navigation, offline viewers and publication figure exports."""

import threading
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QImage, QRawFont

from lab_control.application.processing import ProcessingService
from lab_control.application.processing_figures import generate_pngs
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceIdentity, Measurement, MeasurementBatch
from lab_control.domain.oscilloscope import COLORS, ScopeDisplay
from lab_control.storage.layout import atomic_json
from lab_control.storage.paths import BatchSaveContext, SaveContext
from lab_control.storage.processing import current_result, process_device, result_home
from lab_control.storage.processing_results import ResultReader
from lab_control.storage.recording import SegmentWriter
from lab_control.storage.recording_catalog import read_device, read_json, scan_recordings
from lab_control.storage.repository import Repository
from lab_control.ui.pages.data_processing import DataProcessingPage
from lab_control.ui.pages.processing_viewer import ProcessingViewer
from lab_control.ui.real_window import RealWindow
from lab_control.ui.widgets.publication_figure import export_figure, prepare_publication_font


def make_record(root, *, array=False, batch=None, count=5):
    identity = DeviceIdentity("RIGOL" if array else "Joinwit", "DHO924" if array else "JW8105", "UI-FIXTURE")
    context = SaveContext(
        root,
        "general",
        "continuous",
        identity=identity,
        adapter_id="rigol.dho924" if array else "joinwit.jw8105",
        batch=batch,
        started_at=datetime.fromisoformat("2026-10-31T10:00:00+08:00"),
    )
    context.prepare()
    writer = SegmentWriter(context.raw_folder)
    for i in range(count):
        x = np.linspace(0, 1e-5, 1000) if array else None
        samples = [
            Measurement(
                "waveform" if array else "power",
                "V" if array else "dBm",
                np.sin(x * 1e6) + i if array else [-10 + i],
                channel,
                x,
                "s" if array else "",
            )
            for channel in (1, 2)
        ]
        writer.append(
            i + 1,
            MeasurementBatch(
                identity,
                tuple(samples),
                {},
                timestamp=f"2026-10-31T02:00:{i:02d}+00:00",
                timing={"recording_elapsed_seconds": i * 3.5},
            ),
        )
    writer.close()
    atomic_json(
        context.raw_folder / "summary.json",
        {"committed": count, "binary_done": True, "analysis_state": "complete"},
    )
    return context


@pytest.fixture
def service(tmp_path):
    repository = Repository(tmp_path / "runtime")
    service = ProcessingService(repository)
    yield service
    service.close()
    repository.close()


def open_view(qtbot, service, context):
    result = process_device(
        read_device(context.folder),
        context.batch.folder if context.batch else context.folder,
        threading.Event(),
    )
    viewer = ProcessingViewer(service)
    qtbot.addWidget(viewer)
    viewer.resize(1100, 600)
    viewer.show()
    viewer.open_result(result)
    qtbot.waitUntil(lambda: bool(viewer.plot_data), timeout=5000)
    return viewer


def settled(viewer):
    return viewer.future is None and viewer.pending is None and not viewer.range_timer.isActive()


def test_scalar_full_range_channels_watts_and_no_timeline(qtbot, service, tmp_path):
    viewer = open_view(qtbot, service, make_record(tmp_path / "data"))
    assert viewer.plot_data[0][1][-1] == 14
    assert len(viewer.plot_data) == 2
    assert not viewer.timeline_row.isVisible()
    viewer.channels.item(1).setCheckState(Qt.CheckState.Unchecked)
    viewer.watts.setChecked(True)
    qtbot.waitUntil(lambda: len(viewer.plot_data) == 1 and viewer.plot_data[0][2][0] > 0)
    assert viewer.plot_data[0][2][0] == pytest.approx(1e-4)
    assert viewer.info["series"][0]["unit"] == "dBm"
    viewer.plot.setXRange(4, 11, padding=0)
    qtbot.waitUntil(lambda: viewer.plot_data[0][1][-1] < 14)
    viewer.fit_all()
    qtbot.waitUntil(lambda: viewer.plot_data[0][1][-1] == 14)


@pytest.mark.parametrize("count", [1, 5])
def test_frame_timeline_single_frame_and_random_access(qtbot, service, tmp_path, count):
    viewer = open_view(qtbot, service, make_record(tmp_path / "data", array=True, count=count))
    assert viewer.timeline_row.isVisible()
    assert viewer.slider.maximum() == count and viewer.slider.isEnabled() == (count > 1)
    viewer.slider.setValue(count)
    qtbot.waitUntil(lambda: f"帧 {count} / {count}" in viewer.status.text())
    assert viewer.plot_data[0][2][0] == count - 1
    assert not viewer.next.isEnabled()


def test_latest_frame_request_discards_outdated_load(qtbot, service, tmp_path, monkeypatch):
    viewer = open_view(qtbot, service, make_record(tmp_path / "data", array=True))
    released, entered = threading.Event(), threading.Event()
    original = ResultReader.frame
    requests = []

    def blocked(self, sequence):
        requests.append(sequence)
        if sequence == 2:
            entered.set()
            released.wait(5)
        return original(self, sequence)

    monkeypatch.setattr(ResultReader, "frame", blocked)
    viewer.slider.setValue(2)
    qtbot.waitUntil(entered.is_set)
    viewer.slider.setValue(3)
    viewer.slider.setValue(4)
    viewer.slider.setValue(5)
    released.set()
    qtbot.waitUntil(lambda: "帧 5 / 5" in viewer.status.text())
    assert requests == [2, 5]
    assert viewer.plot_data[0][2][0] == 4


def test_history_classification_device_detail_process_and_corrupt_result(qtbot, service, tmp_path):
    single = make_record(tmp_path / "data")
    batch = BatchSaveContext(single.root)
    make_record(single.root, batch=batch)
    make_record(single.root, array=True, batch=batch)
    service.catalog.add_root(single.root)
    page = DataProcessingPage(SimpleNamespace(runtime=SimpleNamespace(processing=service)))
    qtbot.addWidget(page)
    page.resize(1400, 700)
    page.show()
    page.set_scope("single_device")
    qtbot.waitUntil(lambda: page.tree.topLevelItemCount() == 1)
    entry = next(e for e in page.entries if e.scope == "single_device")
    page.open_entry(entry)
    assert page.devices.count() == 1 and "尚无处理" in page.viewer.status.text()
    page.process(entry)
    qtbot.waitUntil(lambda: bool(page.viewer.plot_data), timeout=5000)
    page.viewer.channels.item(1).setCheckState(Qt.CheckState.Unchecked)
    qtbot.waitUntil(
        lambda: (
            len(page.viewer.plot_data) == 1
            and page.viewer.future is None
            and page.viewer.pending is None
            and not page.viewer.range_timer.isActive()
        )
    )
    page.viewer.export_current()
    qtbot.waitUntil(lambda: not page.operations and "论文插图已保存" in page.message.text())
    assert page.viewer.channels.item(1).checkState() == Qt.CheckState.Unchecked
    result = current_result(result_home(entry.folder, entry.devices[0]))
    (result.parent / "current.json").write_text("invalid")
    page.update_states()  # A bad result index must not crash the timer.
    assert "损坏" in page.rows[entry.key][1].text(3)
    page.set_scope("batch")
    qtbot.waitUntil(lambda: page.tree.topLevelItemCount() == 1 and page.scope == "batch")
    entry = next(e for e in page.entries if e.scope == "batch")
    page.open_entry(entry)
    assert page.devices.count() == 2


def test_navigation_offers_offline_tabs_without_connected_devices(qtbot, tmp_path):
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    try:
        window.navigate("数据处理")
        assert [window.submenu.tabText(i) for i in range(window.submenu.count())] == [
            "单设备采集",
            "批量采集",
        ]
        page = window.stack.currentWidget()
        window.submenu.setCurrentIndex(1)
        assert window.stack.currentWidget() is page and page.scope == "batch"
        assert not runtime.sessions.sessions
    finally:
        window.closing = True
        window.timer.stop()
        window.controller.close()
        window.agent_panel.shutdown()
        window.storage_panel.shutdown()
        window.environment_panel.shutdown()
        window.bridge.worker.shutdown(wait=True)
        runtime.close()


@pytest.mark.parametrize("extension", ["png", "svg", "pdf"])
def test_white_publication_export_preserves_data_and_dimensions(qapp, tmp_path, extension):
    font = QRawFont.fromFont(QFont(prepare_publication_font(), 8))
    assert font.supportsCharacter(ord("A")) and font.supportsCharacter(ord("0"))
    x = np.linspace(0, 100, 1000)
    y = np.sin(x / 10)
    before = y.copy()
    output = tmp_path / f"figure.{extension}"
    export_figure(output, [("CH 1", x, y)], "Elapsed time (s)", "Power (dBm)", width_mm=89)
    assert output.stat().st_size > 500
    np.testing.assert_array_equal(y, before)
    if extension == "png":
        image = QImage(str(output))
        assert image.width() == round(89 / 25.4 * 600)
        assert image.pixelColor(0, 0).name() == "#ffffff"
        assert image.dotsPerMeterX() == round(600 / 0.0254)
    if extension == "svg":
        text = output.read_text()
        assert 'font-family="Arial"' in text and "Elapsed time (s)" in text
    with pytest.raises(FileExistsError):
        export_figure(output, [("CH 1", x, y)], "Time", "Power")


def test_figure_action_writes_beside_processed_result(qtbot, service, tmp_path):
    context = make_record(tmp_path / "data")
    viewer = open_view(qtbot, service, context)
    qtbot.waitUntil(lambda: viewer.future is None and viewer.pending is None)
    entry = scan_recordings((context.root,))[0]
    with qtbot.waitSignal(viewer.export_requested) as emitted:
        viewer.export_current()
    result = service.export_figure(entry, emitted.args[0]).result(10)
    from pathlib import Path

    path = Path(result["figure"])
    assert path.is_relative_to(context.folder / "数据处理")
    assert path.with_suffix(".json").exists()


def test_figure_envelope_preserves_spikes_and_missing_segments():
    from lab_control.ui.widgets.publication_figure import _envelope

    x = np.arange(100_000, dtype=float)
    y = np.zeros_like(x)
    y[55555], y[77777] = 100, -100
    y[20000:20010] = np.nan
    xx, yy = _envelope(x, y, target=400)
    assert len(xx) < 800 and (xx[0], xx[-1]) == (0, 99999)
    assert np.nanmax(yy) == 100 and np.nanmin(yy) == -100
    assert np.isnan(xx).any()


def test_spectrum_axis_keeps_nm_without_hidden_si_scale(qtbot, service, tmp_path):
    viewer = open_view(qtbot, service, make_record(tmp_path / "data", array=True))
    viewer.group.blockSignals(True)
    viewer.group.clear()
    viewer.group.addItem("光谱 · dBm", ("spectrum", "dBm", "nm"))
    viewer.group.blockSignals(False)
    viewer._fit_pending = True
    viewer._draw([("CH 1", np.linspace(1300, 1600, 1000), np.linspace(-80, -10, 1000))])
    assert viewer.plot.getAxis("bottom").autoSIPrefixScale == 1
    assert viewer.plot.getAxis("bottom").labelUnits == "nm"


def test_fit_recovers_microsecond_waveform_after_far_pan_and_channel_toggle(qtbot, service, tmp_path):
    viewer = open_view(qtbot, service, make_record(tmp_path / "data", array=True))
    qtbot.waitUntil(lambda: settled(viewer))
    for offscreen in ((100, 101), (-1e6, -1e6 + 100), (0, 1e-9)):
        viewer.plot.setRange(xRange=offscreen, yRange=(10000, 10001), padding=0)
        qtbot.wait(20)
        viewer.fit_all()
        qtbot.waitUntil(lambda: settled(viewer))
        x, y = viewer.plot_data[0][1:]
        xr, yr = viewer.plot.viewRange()
        assert xr[0] < x.min() and xr[1] > x.max()
        assert xr[1] - xr[0] == pytest.approx(np.ptp(x) * 1.08)
        assert yr[0] < y.min() and yr[1] > y.max()
        assert yr[1] - yr[0] == pytest.approx(np.ptp(y) * 1.08)
    color = viewer.plot.listDataItems()[1].opts["pen"].color().name()
    viewer.channels.item(0).setCheckState(Qt.CheckState.Unchecked)
    qtbot.waitUntil(lambda: len(viewer.plot_data) == 1 and settled(viewer))
    assert viewer.plot.listDataItems()[0].opts["pen"].color().name() == color == COLORS[1]


def test_scope_display_uses_recorded_offsets_but_cursor_and_exports_stay_physical(qtbot, service, tmp_path):
    context = make_record(tmp_path / "data", array=True)
    viewer = open_view(qtbot, service, context)
    qtbot.waitUntil(lambda: settled(viewer))
    frame = viewer.reader.frame(1)
    specs = [ScopeDisplay(2, 1.583138, 1e-6, 0), ScopeDisplay(5, 9, 1e-6, 0)]
    for sample, spec in zip(frame["measurements"], specs, strict=True):
        sample["metadata"]["scope_display"] = spec.metadata()
    viewer._loaded("frame", frame)
    assert viewer.scope_view.isVisible() and viewer.scope_view.isChecked()
    for i, curve in enumerate(viewer.plot.listDataItems()):
        physical = frame["measurements"][i]["values"]
        np.testing.assert_array_equal(
            curve.yData, (physical - specs[i].reference_value) / specs[i].units_per_div
        )
        np.testing.assert_array_equal(viewer.plot_data[i][2], physical)
        np.testing.assert_array_equal(viewer.cursor_data[i][2], physical)
    coordinates = [curve.yData for curve in viewer.plot.listDataItems()]
    lo, hi = min(y.min() for y in coordinates), max(y.max() for y in coordinates)
    bounds = viewer.plot.viewRange()[1]
    assert bounds[0] < lo and bounds[1] > hi and bounds[1] - bounds[0] == pytest.approx((hi - lo) * 1.08)
    with qtbot.waitSignal(viewer.export_requested) as emitted:
        viewer.export_current()
    result = service.export_figure(scan_recordings((context.root,))[0], emitted.args[0]).result(10)
    assert len(result["figures"]) == 2
    for path, sample in zip(result["figures"], frame["measurements"], strict=True):
        metadata = read_json(Path(path).with_suffix(".json"))
        assert metadata["channels"] == [f"CH {sample['channel']}"]
    viewer.scope_view.setChecked(False)
    qtbot.waitUntil(lambda: settled(viewer))
    assert viewer.plot.getAxis("left").labelUnits == "V"


def test_scalar_fit_covers_full_values_after_zoom_and_keeps_gaps(qtbot, service, tmp_path):
    viewer = open_view(qtbot, service, make_record(tmp_path / "data"))
    qtbot.waitUntil(lambda: settled(viewer))
    viewer.plot.setRange(xRange=(1000, 1001), yRange=(10, 11), padding=0)
    qtbot.waitUntil(lambda: settled(viewer) and viewer.plot_data[0][1].size == 0)
    viewer.fit_all()
    qtbot.waitUntil(lambda: settled(viewer) and viewer.plot_data[0][1].size > 0)
    xr, yr = viewer.plot.viewRange()
    assert xr[0] < 0 and xr[1] > 14 and xr[1] - xr[0] < 16
    assert yr[0] < -10 and yr[1] > -6 and yr[1] - yr[0] < 5
    viewer._fit_pending = True
    viewer._draw([("CH 1", np.array([0, 1, np.nan, 2, 10]), np.array([5, 5, np.nan, 5, 5]))])
    curve = viewer.plot.listDataItems()[0]
    assert not curve.opts["clipToView"] and not curve.opts["autoDownsample"]
    assert np.isnan(curve.yData).any()
    assert viewer.plot.viewRange()[1][1] - viewer.plot.viewRange()[1][0] < 0.01


def test_date_folding_persists_refresh_and_open_folder_targets_whole_capture(
    qtbot, service, tmp_path, monkeypatch
):
    context = make_record(tmp_path / "data")
    batch = BatchSaveContext(context.root)
    make_record(context.root, batch=batch)
    page = DataProcessingPage(SimpleNamespace(runtime=SimpleNamespace(processing=service)))
    qtbot.addWidget(page)
    page.entries = scan_recordings((context.root,))
    entry = next(e for e in page.entries if e.scope == "single_device")
    yesterday = replace(entry, folder=entry.folder.parent / "other", started="2026-10-30T23:50:00-05:00")
    page.entries += (yesterday,)
    page.rebuild()
    assert page.tree.topLevelItemCount() == 2
    group = page.tree.topLevelItem(0)
    assert group.childCount() == 1 and "2026-10-31" in group.text(0)
    page.tree.itemClicked.emit(group, 0)
    assert page.entry is None
    group.setExpanded(False)
    page.rebuild()
    assert not page.tree.topLevelItem(0).isExpanded()
    opened = []
    monkeypatch.setattr(
        "lab_control.ui.pages.data_processing.QDesktopServices.openUrl",
        lambda url: opened.append(url.toLocalFile()) or True,
    )
    page.rows[entry.key][2][-1].click()
    assert Path(opened[-1]) == entry.folder
    batch_entry = next(e for e in page.entries if e.scope == "batch")
    page.open_entry(batch_entry)
    page.source_folder_button.click()
    assert Path(opened[-1]) == batch_entry.folder


@pytest.mark.parametrize("array", [False, True])
def test_processing_automatically_produces_pngs_reuses_and_repairs(qapp, service, tmp_path, array):
    context = make_record(tmp_path / "data", array=array, count=3)
    entry = scan_recordings((context.root,))[0]
    result = service.process(entry).result(20)
    assert not result[0]["error"]
    folder = Path(result[0]["result"])
    manifest = read_json(folder / "png.json")
    assert len(manifest["files"]) == (6 if array else 1)
    if array:
        assert all(len(item["channels"]) == 1 for item in manifest["files"])
        assert {item["frame"] for item in manifest["files"]} == {1, 2, 3}
    for item in manifest["files"]:
        image = QImage(str(folder / manifest["directory"] / item["path"]))
        assert not image.isNull() and image.pixelColor(0, 0).name() == "#ffffff"
    service.process(entry).result(20)
    assert read_json(folder / "png.json") == manifest
    path = folder / manifest["directory"] / manifest["files"][0]["path"]
    path.unlink()
    service.process(entry).result(20)
    repaired = read_json(folder / "png.json")
    assert repaired["directory"] != manifest["directory"]
    assert current_result(folder.parent) == folder  # Numeric cache is reused.


def test_png_failure_and_cancellation_keep_published_result(qapp, service, tmp_path, monkeypatch):
    from lab_control.application import processing_figures
    from lab_control.storage.processing import ProcessingCancelled

    context = make_record(tmp_path / "data", array=True, count=2)
    entry = scan_recordings((context.root,))[0]
    result = service.process(entry).result(20)
    folder = Path(result[0]["result"])
    previous = (folder / "png.json").read_bytes()
    monkeypatch.setattr(processing_figures, "FIGURE_VERSION", 2)
    cancel = threading.Event()

    def stop(done, total):
        if done:
            cancel.set()

    with pytest.raises(ProcessingCancelled):
        generate_pngs(folder, cancel, stop)
    assert (folder / "png.json").read_bytes() == previous
    assert not list((folder / "图片").glob(".processing-*"))
    monkeypatch.setattr(
        "lab_control.ui.widgets.publication_figure.export_figure",
        lambda *a, **k: (_ for _ in ()).throw(OSError("PNG write failed")),
    )
    outcome = service.process(entry).result(20)
    assert "PNG write failed" in outcome[0]["error"]
    assert current_result(folder.parent) == folder and (folder / "png.json").read_bytes() == previous
    summary = read_json(context.raw_folder / "summary.json")
    atomic_json(context.raw_folder / "summary.json", {**summary, "error": "changed source metadata"})
    outcome = service.process(entry).result(20)
    assert "PNG write failed" in outcome[0]["error"]
    assert current_result(folder.parent) == folder  # Failed new generation must not replace it either.
    assert not list(folder.parent.glob(".processing-*"))
