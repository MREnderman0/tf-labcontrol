"""Formal-theme screenshots and release-to-visible-frame timing, offline only."""

import argparse
import json
import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory


def run(output: Path, scale: str, benchmark: bool) -> None:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_SCALE_FACTOR"] = scale
    import numpy as np
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    from lab_control.app import load_stylesheet, load_ui_font
    from lab_control.application.config import ConnectionConfig
    from lab_control.application.runtime import ApplicationRuntime
    from lab_control.instruments.devices.rigol_dho924.adapter import DHO924Adapter
    from lab_control.instruments.devices.rigol_dho924.profile import profile
    from lab_control.instruments.devices.rigol_dho924.tests.fake import Peer
    from lab_control.transport.base import ieee_block
    from lab_control.ui.real_window import RealWindow

    app = QApplication([])
    app.setStyle("Fusion")
    app.setFont(QFont(load_ui_font(), 10))
    app.setStyleSheet(load_stylesheet())
    output.mkdir(parents=True, exist_ok=True)
    screenshots, samples = [], []

    def pump(seconds=0.03):
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            app.processEvents()
            time.sleep(0.001)

    def wait(predicate):
        deadline = time.perf_counter() + 15
        while not predicate() and time.perf_counter() < deadline:
            pump(0.003)
        assert predicate(), "Preview/timing condition not reached"

    with TemporaryDirectory(prefix="scope-controls-review-") as folder:
        runtime = ApplicationRuntime(root=Path(folder), hardware=False)
        peer = Peer()
        config = ConnectionConfig("mock://scope-control-preview", "rigol.dho924", "mock")
        runtime.sessions.factory = lambda cfg: DHO924Adapter(peer, profile(), cfg)
        key = runtime.add(config)
        runtime.connect(key).result(5)
        adapter = runtime.sessions.sessions[key].adapter
        native_query = peer.query
        delay = 0.0

        def query(command):
            if delay:
                time.sleep(delay)
            response = native_query(command)
            if command == ":WAV:DATA?":
                ch = int(peer.state[":WAV:SOUR"][-1])
                codes = (
                    32768
                    + np.sin(np.linspace(0, 12, 1000) + ch * 0.5) * 6000
                    + (2.4, 0.8, -0.8, -2.4)[ch - 1] * 7500
                )
                return ieee_block(codes.astype("<u2").tobytes()) + b"\n"
            return response

        peer.query = query
        window = RealWindow(runtime)
        window.show()
        window.navigate("实时监测", "oscilloscope", key)
        page = window.stack.currentWidget()
        try:
            wait(lambda: page.controls and len(page.controls.values) >= 27 and not page.controls.running)
            page.start()
            wait(lambda: page.plot.displayed_frame is not None)
            for width, height in ((1280, 760), (1440, 900)):
                window.resize(width, height)
                for fine in (False, True):
                    page.controls.fine_button.setChecked(fine)
                    pump(0.1)
                    controls = page.controls
                    bottom = controls.auto_button.mapTo(
                        controls, QPoint(0, controls.auto_button.height())
                    ).y()
                    assert bottom < controls.height()
                    assert controls.auto_button.isVisible() and controls.fine_button.isVisible()
                    assert page.plot.plot.viewRange()[1] == [-4, 4]
                    assert all(len(c.getData()[0]) > 0 for c in page.plot.curves.values())
                    name = f"scope-{'fine' if fine else 'normal'}-{width}x{height}-{scale}.png"
                    window.grab().save(str(output / name))
                    screenshots.append(
                        {
                            "file": name,
                            "footer_bottom": bottom,
                            "controls_height": controls.height(),
                            "dpr": window.devicePixelRatioF(),
                        }
                    )
                    if fine:
                        page.plot.preview_parameter(1, "scale", .1)
                        page.plot.preview_parameter(2, "offset", .02)
                        page.plot.preview_parameter(1, "timebase", 2e-6)
                        page.plot.preview_parameter(1, "time_position", 1e-6)
                        pump(.05)
                        assert "估算" in page.plot.meta.text()
                        name = f"scope-preview-{width}x{height}-{scale}.png"
                        window.grab().save(str(output / name))
                        page.plot.clear_preview()
                runtime.monitoring.set_recording(key, True)
                pump(0.1)
                assert not page.controls.fine_button.isEnabled() and not page.controls.auto_button.isEnabled()
                window.grab().save(str(output / f"scope-locked-{width}x{height}-{scale}.png"))
                runtime.monitoring.set_recording(key, False)
                wait(lambda: not runtime.sessions.snapshots()[0]["controlsLocked"])
            if benchmark:
                delay = 0.005
                optimized_apply = adapter.apply
                for round_number in range(1, 4):
                    for optimized in (False, True):
                        adapter.apply = optimized_apply if optimized else adapter._apply_command
                        for index in range(6):
                            wait(
                                lambda: (
                                    not page.controls.running
                                    and page.controls.sliders[1, "offset"].isEnabled()
                                )
                            )
                            old_version = runtime.sessions.snapshots()[0]["configurationVersion"]
                            field = page.controls.sliders[1, "offset"]
                            field.keyboard_step(1 if index % 2 == 0 else -1)
                            start = time.perf_counter()
                            field.commit()  # The same signal emitted on mouse release.
                            wait(lambda: not page.controls.running)
                            control_done = time.perf_counter()
                            wait(lambda: page.plot.displayed_frame.configuration_version > old_version)
                            done = time.perf_counter()
                            if index:
                                samples.append(
                                    {
                                        "round": round_number,
                                        "optimized": optimized,
                                        "sample": index,
                                        "control_and_ui_seconds": control_done - start,
                                        "next_display_seconds": done - control_done,
                                        "total_seconds": done - start,
                                    }
                                )
                groups = []
                for r in range(1, 4):
                    row = {"round": r}
                    for opt in (False, True):
                        values = [
                            s["total_seconds"] for s in samples if s["round"] == r and s["optimized"] == opt
                        ]
                        row["optimized" if opt else "baseline"] = {
                            "median": float(np.median(values)),
                            "p95": float(np.percentile(values, 95)),
                            "max": max(values),
                        }
                    row["reduction"] = 1 - row["optimized"]["median"] / row["baseline"]["median"]
                    assert row["reduction"] >= 0.3 and row["optimized"]["p95"] <= row["baseline"]["p95"]
                    groups.append(row)
                (output / "ui-timing.json").write_text(
                    json.dumps(
                        {
                            "mode": "offline_synthetic",
                            "delay_per_query": delay,
                            "groups": groups,
                            "samples": samples,
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                print(json.dumps(groups, indent=2))
            (output / f"screenshots-{scale}.json").write_text(
                json.dumps(screenshots, indent=2), encoding="utf-8"
            )
        finally:
            window.timer.stop()
            window.controller.close()
            runtime.close()
            window.closing = True
            window.bridge.worker.shutdown()
            window.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--scale", default="1")
    parser.add_argument("--benchmark", action="store_true")
    args = parser.parse_args()
    run(args.output, args.scale, args.benchmark)
