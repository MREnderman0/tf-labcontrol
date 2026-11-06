"""History navigation, offline viewers and publication figure exports."""

import os
import sqlite3
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QImage, QRawFont
from PySide6.QtWidgets import QPushButton

from lab_control.application.processing import ProcessingService
from lab_control.application.processing_figures import generate_pngs
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceIdentity, Measurement, MeasurementBatch
from lab_control.domain.oscilloscope import COLORS, ScopeDisplay
from lab_control.storage.layout import atomic_json
from lab_control.storage.paths import BatchSaveContext, SaveContext
from lab_control.storage.processing import current_result, image_home, process_device, result_home
from lab_control.storage.processing_paths import filesystem_path
from lab_control.storage.processing_results import ResultReader
from lab_control.storage.recording import SegmentWriter
from lab_control.storage.recording_catalog import read_device, read_json, scan_recordings
from lab_control.storage.repository import Repository
from lab_control.ui.pages.data_processing import DataProcessingPage
from lab_control.ui.pages.processing_viewer import ProcessingViewer
from lab_control.ui.real_window import RealWindow
from lab_control.ui.widgets.publication_figure import export_figure, prepare_publication_font


def make_record(
    root, *, array=False, batch=None, count=5, channels=(1, 2), spectrum=False, scope_adapter="rigol.dho924"
):
    array |= spectrum
    scope_maker, scope_model, scope_quantity = {
        "rigol.dho924": ("RIGOL", "DHO924", "waveform"),
        "rs.rtm3004": ("Rohde&Schwarz", "RTM3004", "voltage"),
    }[scope_adapter]
    identity = (
        DeviceIdentity("Ceyear", "CY6362D", "UI-FIXTURE")
        if spectrum
        else DeviceIdentity(
            scope_maker if array else "Joinwit", scope_model if array else "JW8105", "UI-FIXTURE"
        )
    )
    context = SaveContext(
        root,
        "general",
        "continuous",
        identity=identity,
        adapter_id="ceyear.6362d" if spectrum else scope_adapter if array else "joinwit.jw8105",
        batch=batch,
        started_at=datetime.fromisoformat("2026-10-31T10:00:00+08:00"),
    )
    context.prepare()
    writer = SegmentWriter(context.raw_folder)
    for i in range(count):
        x = np.linspace(1300, 1600, 1000) if spectrum else np.linspace(0, 1e-5, 1000) if array else None
        samples = [
            Measurement(
                "spectrum" if spectrum else scope_quantity if array else "power",
                "dBm" if spectrum else "V" if array else "dBm",
                np.sin(x * 1e6) + i if array else [-10 + i],
                channel,
                x,
                "nm" if spectrum else "s" if array else "",
            )
            for channel in channels
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


@pytest.mark.skipif(os.name != "nt", reason="Windows nested processing cache paths")
def test_nested_windows_cache_png_publication_and_reuse(qtbot, service, tmp_path):
    root = tmp_path / ("nested-" * 7)
    context = make_record(root, array=True, count=1, channels=(1, 2), scope_adapter="rs.rtm3004")
    (entry,) = scan_recordings((root,))
    original = {p.name: p.read_bytes() for p in context.raw_folder.iterdir() if p.is_file()}
    (outcome,) = service.process(entry).result(20)
    assert not outcome["error"]
    result = current_result(result_home(entry.folder, entry.devices[0]))
    reader = ResultReader(result)
    manifest = read_json(filesystem_path(result) / "png.json")
    assert len(manifest["files"]) == 2
    nested = result / manifest["directory"] / manifest["files"][0]["path"]
    assert len(str(nested)) > 260 and filesystem_path(nested).is_file()
    assert reader.info()["frames"] == 1 and reader.frame(1)["measurements"][0]["unit"] == "V"
    with reader.connect() as db, pytest.raises(sqlite3.OperationalError, match="readonly"):
        db.execute("DELETE FROM frames")
    assert not str(result).startswith("\\\\?\\")
    assert service.process(entry).result(20)[0]["result"] == str(result)
    assert original == {p.name: p.read_bytes() for p in context.raw_folder.iterdir() if p.is_file()}
    assert len(list(image_home(entry.folder, entry.devices[0]).rglob("*.png"))) == 2


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
    assert not any("导出" in button.text() for button in page.viewer.findChildren(QPushButton))
    page.process_button.click()
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
    qtbot.waitUntil(lambda: not page.operations and "PNG 图片已自动保存" in page.message.text())
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


@pytest.mark.parametrize("array", [False, True])
@pytest.mark.parametrize("channels", [(1,), (1, 2)])
@pytest.mark.parametrize("count", [1, 3])
def test_image_only_model_folder_layout(qapp, service, tmp_path, array, channels, count):
    context = make_record(tmp_path / "data", array=array, count=count, channels=channels)
    entry = scan_recordings((context.root,))[0]
    result = service.process(entry).result(20)
    assert not result[0]["error"]
    output = image_home(entry.folder, entry.devices[0])
    paths = [p.relative_to(output) for p in output.rglob("*") if p.is_file()]
    assert len(paths) == (len(channels) * count if array else 1)
    for path in paths:
        assert path.suffix == ".png"
        assert len(path.parts) == 1 + (array and len(channels) > 1) + (array and count > 1)
        if array and len(channels) > 1:
            assert path.parts[0] in {f"CH{channel}" for channel in channels}
        if array and count > 1:
            assert path.parent.name == "帧序列"
    assert Path(result[0]["result"]).is_relative_to(entry.folder / ".processing-cache")
    if os.name == "nt":
        assert (entry.folder / ".processing-cache").stat().st_file_attributes & 0x02


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


def test_isolated_samples_are_visible_without_connecting_gaps(qapp, tmp_path):
    output = tmp_path / "isolated.png"
    x, y = np.array([0, np.nan, 1, np.nan, 2]), np.array([0, np.nan, 2, np.nan, 1])
    export_figure(output, [("CH 1", x, y)], "Time (s)", "Frequency (Hz)")
    image = QImage(str(output)).convertToFormat(QImage.Format.Format_RGBA8888)
    pixels = np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.width(), 4)
    # Exclude the legend; three separated points must still paint measured data.
    chart = pixels[image.height() // 6 : -image.height() // 8, image.width() // 7 :]
    assert np.count_nonzero(np.all(chart[:, :, :3] == [0, 114, 178], axis=2)) > 50


def test_multichannel_spectrum_combines_each_frame_into_one_png(qapp, service, tmp_path):
    context = make_record(tmp_path / "data", spectrum=True, count=2)
    entry = scan_recordings((context.root,))[0]
    result = service.process(entry).result(20)
    assert not result[0]["error"]
    manifest = read_json(Path(result[0]["result"]) / "png.json")
    assert len(manifest["files"]) == 2
    for item in manifest["files"]:
        assert item["channels"] == ["CH 1", "CH 2"]
        assert Path(item["path"]).parent == Path("帧序列")


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


@pytest.mark.parametrize("scope_adapter", ["rigol.dho924", "rs.rtm3004"])
def test_scope_display_uses_recorded_offsets_but_cursor_and_pngs_stay_physical(
    qtbot, service, tmp_path, monkeypatch, scope_adapter
):
    context = make_record(tmp_path / "data", array=True, scope_adapter=scope_adapter)
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
    rendered = []

    def render(path, curves, *args, **kwargs):
        rendered.extend(curves)
        export_figure(path, curves, *args, **kwargs)

    monkeypatch.setattr("lab_control.ui.widgets.publication_figure.export_figure", render)
    result = service.process(scan_recordings((context.root,))[0]).result(20)
    assert not result[0]["error"]
    assert len(rendered) == 10
    for curve, sample in zip(rendered[:2], frame["measurements"], strict=True):
        assert curve[0] == f"CH {sample['channel']}"
        np.testing.assert_array_equal(curve[2], sample["values"])
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
    make_record(context.root, array=True, batch=batch)
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
    result = service.process(batch_entry).result(20)
    assert all(not item["error"] for item in result)
    page.devices.setCurrentIndex(1)
    page.open_results_folder()
    assert Path(opened[-1]) == image_home(batch_entry.folder, batch_entry.devices[1])


@pytest.mark.parametrize("array", [False, True])
def test_processing_automatically_produces_pngs_reuses_and_repairs(qapp, service, tmp_path, array):
    context = make_record(tmp_path / "data", array=array, count=3)
    entry = scan_recordings((context.root,))[0]
    result = service.process(entry).result(20)
    assert not result[0]["error"]
    folder = Path(result[0]["result"])
    manifest = read_json(folder / "png.json")
    assert len(manifest["files"]) == (6 if array else 1)
    if not array:
        assert manifest["files"][0]["channels"] == ["CH 1", "CH 2"]
    if array:
        assert all(len(item["channels"]) == 1 for item in manifest["files"])
        assert {item["frame"] for item in manifest["files"]} == {1, 2, 3}
    for item in manifest["files"]:
        image = QImage(str(folder / manifest["directory"] / item["path"]))
        assert not image.isNull() and image.pixelColor(0, 0).name() == "#ffffff"
    service.process(entry).result(20)
    assert read_json(folder / "png.json") == manifest
    public = image_home(entry.folder, entry.devices[0])
    visible = public / manifest["files"][0]["path"]
    visible.unlink()
    service.process(entry).result(20)
    assert visible.is_file() and read_json(folder / "png.json") == manifest
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
    monkeypatch.setattr(processing_figures, "FIGURE_VERSION", processing_figures.FIGURE_VERSION + 1)
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


def test_legacy_results_remain_readable_and_reprocessing_publishes_only_images(qapp, service, tmp_path):
    from lab_control.storage.processing import fingerprints

    context = make_record(tmp_path / "data", channels=(1,))
    entry = scan_recordings((context.root,))[0]
    device = entry.devices[0]
    original = fingerprints(device, threading.Event())
    cached = process_device(device, entry.folder, threading.Event())
    public = image_home(entry.folder, device)
    public.parent.mkdir()
    cached.parent.rename(public)  # The former current.json/result-* layout.
    old = public / cached.name
    (public / "user-notes.txt").write_text("keep my notes")
    assert current_result(result_home(entry.folder, device)) == old
    result = service.process(entry).result(20)
    assert not result[0]["error"]
    assert [p.suffix for p in public.iterdir()] == [".png"]
    home = result_home(entry.folder, device)
    backup = next(home.glob("previous-images-*"))
    assert (backup / "user-notes.txt").read_text() == "keep my notes"
    assert (backup / old.name / "data.sqlite3").exists()
    assert fingerprints(device, threading.Event()) == original
    assert scan_recordings((entry.folder,)) == (entry,)


@pytest.mark.parametrize("failure", ["cancel_copy", "rename"])
def test_image_publication_preserves_previous_tree_on_cancel_or_failure(
    qapp, service, tmp_path, monkeypatch, failure
):
    from lab_control.application import processing_figures
    from lab_control.storage.processing import ProcessingCancelled

    context = make_record(tmp_path / "data")
    entry = scan_recordings((context.root,))[0]
    result = service.process(entry).result(20)
    folder = Path(result[0]["result"])
    public = image_home(entry.folder, entry.devices[0])
    (public / "user-notes.txt").write_text("preserve")
    before = {p.relative_to(public): p.read_bytes() for p in public.rglob("*") if p.is_file()}
    cancel = threading.Event()
    copy, rename = processing_figures.shutil.copy2, Path.rename

    def cancel_copy(*args, **kwargs):
        value = copy(*args, **kwargs)
        cancel.set()
        return value

    def fail_rename(path, target):
        if path.name.startswith(".processing-publish-"):
            raise OSError("publish failed")
        return rename(path, target)

    if failure == "cancel_copy":
        monkeypatch.setattr(processing_figures.shutil, "copy2", cancel_copy)
    else:
        monkeypatch.setattr(Path, "rename", fail_rename)
    with pytest.raises(ProcessingCancelled if failure == "cancel_copy" else OSError):
        processing_figures.publish_pngs(folder, public, cancel)
    assert {p.relative_to(public): p.read_bytes() for p in public.rglob("*") if p.is_file()} == before
    assert not list(folder.parent.glob(".processing-*"))


def test_dense_noisy_spectrum_png_keeps_qt_responsive(tmp_path):
    # Use a separate interpreter so a regression inside Qt's native path
    # stroker is bounded by the subprocess timeout instead of hanging pytest.
    script = """
import sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QLabel
from lab_control.ui.widgets.publication_figure import export_figure, prepare_publication_font
app = QApplication([])
label = QLabel('Rendering spectrum')
label.show()
prepare_publication_font()
x = np.linspace(600, 1700, 50001)
y = np.random.default_rng(11).normal(-70, 3, x.size)
beats = []
future = None
worker = ThreadPoolExecutor(1)
def draw():
    for index in range(3):
        export_figure(Path(sys.argv[1]) / f'{index}.png', [('CH 1', x, y)], 'Wavelength (nm)', 'Power (dBm)')
def start():
    global future
    future = worker.submit(draw)
def tick():
    beats.append(time.monotonic())
    label.setText(f'Heartbeat {len(beats)}')
    if future is not None and future.done():
        app.quit()
timer = QTimer()
timer.timeout.connect(tick)
timer.start(20)
QTimer.singleShot(100, start)
app.exec()
future.result()
worker.shutdown()
assert len(beats) > 10
assert max(np.diff(beats)) < 0.75, max(np.diff(beats))
print('responsive')
"""
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    # The base executable avoids the Windows venv launcher leaving a child
    # behind on timeout. PYTHONPATH still uses this project's installed libs.
    result = subprocess.run(
        [sys._base_executable, "-c", script, str(tmp_path)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(list(tmp_path.glob("*.png"))) == 3


def test_png_can_be_cancelled_between_raster_batches(qapp, tmp_path):
    from lab_control.storage.processing import ProcessingCancelled

    checkpoints = 0

    def cancel():
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints == 3:
            raise ProcessingCancelled("cancel rendering")

    x = np.arange(12000)
    y = np.random.default_rng(11).normal(size=x.size)
    path = tmp_path / "cancel.png"
    with pytest.raises(ProcessingCancelled):
        export_figure(path, [("CH 1", x, y)], "X", "Y", check_cancel=cancel)
    assert not path.exists()
