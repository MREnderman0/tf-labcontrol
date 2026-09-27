"""Render fixed grids and real Mock recording/drain states; never open hardware."""

import argparse
import json
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory


def render(output: Path, scale: str) -> None:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_SCALE_FACTOR"] = scale
    import numpy as np
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication, QToolButton

    from lab_control.app import load_stylesheet, load_ui_font
    from lab_control.application.config import ConnectionConfig
    from lab_control.application.runtime import ApplicationRuntime
    from lab_control.ui.real_window import RealWindow

    app = QApplication([])
    app.setStyle("Fusion")
    app.setFont(QFont(load_ui_font(), 10))
    app.setStyleSheet(load_stylesheet())
    output.mkdir(parents=True, exist_ok=True)
    rows = []

    def pump(seconds=0.1):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            app.processEvents()
            time.sleep(0.005)

    def wait(predicate):
        deadline = time.monotonic() + 10
        while not predicate() and time.monotonic() < deadline:
            pump(0.03)
        assert predicate(), "Preview did not reach the expected state"

    def grid_evidence(plot):
        assert plot.plot.viewRange()[1] == [-4, 4]
        assert not any(plot.plot.getViewBox().autoRangeEnabled())
        assert all(len(c.getData()[0]) > 1 for c in plot.curves.values())
        raster = plot.plot.grab().toImage()
        ratio = raster.devicePixelRatio()
        view = plot.plot.getViewBox()
        left, right = plot.grid_range
        detected = []
        for y in range(-3, 4):
            hits = 0
            for x in np.linspace(left + (right - left) * 0.1, right - (right - left) * 0.1, 200):
                point = plot.plot.mapFromScene(view.mapViewToScene(QPointF(float(x), y)))
                px, py = round(point.x() * ratio), round(point.y() * ratio)
                baseline = raster.pixelColor(px, py + round(4 * ratio)).lightness()
                peak = max(raster.pixelColor(px, py + d).lightness() for d in range(-2, 3))
                hits += peak - baseline > 10
            if hits >= 25:
                detected.append(y)
        assert len(detected) >= 3, detected
        return {"y_range": [-4, 4], "raster_visible_internal_lines": detected}

    with TemporaryDirectory(prefix="recording-grid-review-") as directory:
        runtime = ApplicationRuntime(root=Path(directory), hardware=False)
        categories = (("oscilloscope", "lecroy.9404m"), ("spectrum_analyzer", "rigol.dsa815"))
        keys = {}
        for category, adapter_id in categories:
            key = runtime.add(ConnectionConfig(f"mock://{category}-preview", adapter_id, "mock"))
            runtime.connect(key).result(5)
            keys[category] = key
        adapter = runtime.sessions.sessions[keys["oscilloscope"]].adapter
        adapter.values["1.timebase"] = {"value": 1e-6}
        for ch, div in enumerate((0.02, 0.05, 0.1, 0.2), 1):
            adapter.values[f"{ch}.scale"] = {"value": div}
        acquire = adapter.acquire

        def curves(request):
            batch = acquire(replace(request, points=5002))
            return replace(
                batch,
                measurements=tuple(
                    replace(
                        m,
                        axis=np.linspace(-5e-6, 5e-6, 5002),
                        values=(
                            np.sin(np.linspace(0, 20, 5002) + m.channel * 0.3) * 0.7
                            + (2.4, 0.8, -0.8, -2.4)[m.channel - 1]
                        )
                        * adapter._value(m.channel, "scale"),
                    )
                    for m in batch.measurements
                ),
            )

        adapter.acquire = curves
        release = threading.Event()
        entered = []
        block = threading.Event()
        factory = runtime.repository.create_recording

        def create(mode, **options):
            recorder = factory(mode, **options)
            append = recorder.segments.append
            writing = threading.Event()
            entered.append(writing)

            def held(sequence, batch):
                if block.is_set():
                    writing.set()
                    assert release.wait(30), "Preview binary write was not released"
                return append(sequence, batch)

            recorder.segments.append = held
            return recorder

        runtime.repository.create_recording = create
        window = RealWindow(runtime)
        window.show()
        pages = {}
        try:
            for category, _ in categories:
                window.navigate("实时监测", category, keys[category])
                page = window.stack.currentWidget()
                pages[category] = page
                wait(lambda: page.controls is not None and not page.controls.running)
                if category == "oscilloscope":
                    wait(lambda: len(page.controls.values) == len(page.controls.keys()))
                    assert page.controls.channels() == (1, 2, 3, 4)
                page.timing.cadence.setCurrentIndex(page.timing.cadence.findData("interval"))
                page.toggle_monitoring()
                wait(lambda: page.plot.displayed_frame is not None)
                if category == "oscilloscope":
                    assert len(page.plot.curves) == 4
                page.pin()

            for width, height in ((1280, 760), (1440, 900)):
                window.resize(width, height)
                for phase in ("idle", "recording", "draining"):
                    if phase == "recording":
                        for key in keys.values():
                            runtime.monitoring.set_recording(key, True)
                        pump(0.6)
                    elif phase == "draining":
                        block.set()
                        wait(lambda: len(entered) == 2 and all(event.is_set() for event in entered))
                        for key in keys.values():
                            runtime.monitoring.set_recording(key, False)
                    for location in (*keys, "overview"):
                        window.navigate(
                            "实时监测", "" if location == "overview" else location, keys.get(location, "")
                        )
                        pump(0.2)
                        window.controller.poll()
                        evidence = {}
                        if location == "overview":
                            for card in window.dashboard.cards.values():
                                card.refresh()
                                assert card.property("recordingPhase") == phase
                                if card.spec["device_id"] == keys["oscilloscope"]:
                                    evidence = grid_evidence(card.plot)
                        else:
                            page = pages[location]
                            assert page.chart.property("recordingPhase") == phase
                            tools = page.findChild(QToolButton, "curveTools")
                            assert page.record_button.geometry().right() < tools.geometry().left()
                            if location == "oscilloscope":
                                page.plot.plot.plotItem.autoBtnClicked()
                                evidence = grid_evidence(page.plot)
                        name = f"{location}-{phase}-{width}x{height}-{scale}.png"
                        assert window.grab().save(str(output / name))
                        rows.append({"file": name, "phase": phase, **evidence})
                block.clear()
                release.set()
                wait(
                    lambda: all(
                        t["recording_state"]["binary_done"] for t in runtime.monitoring.snapshots().values()
                    )
                )
                entered.clear()
                release.clear()
        finally:
            release.set()
            window.timer.stop()
            window.controller.close()
            runtime.close()
            window.closing = True
            window.close()
            window.bridge.worker.shutdown()
    (output / f"render-{scale}.json").write_text(
        json.dumps({"hardware_access": False, "scale": scale, "renders": rows}, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".tools/recording-grid/screens"))
    parser.add_argument("--scale", choices=("1", "1.5"), default="1")
    args = parser.parse_args()
    render(args.output, args.scale)
