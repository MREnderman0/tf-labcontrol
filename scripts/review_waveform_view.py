"""Render short-timebase monitoring with Mock data or offline archived waveforms."""

import argparse
import json
import os
import time
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory


def render(output: Path, scale: str, waveform_file: Path | None = None) -> None:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_SCALE_FACTOR"] = scale
    import numpy as np
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    from lab_control.app import load_stylesheet, load_ui_font
    from lab_control.application.config import ConnectionConfig
    from lab_control.application.runtime import ApplicationRuntime
    from lab_control.instruments.devices.lecroy_waverunner9404m.protocol import waveform
    from lab_control.ui.real_window import RealWindow

    sample = waveform(waveform_file.read_bytes(), 1) if waveform_file else None
    app = QApplication([])
    app.setStyle("Fusion")
    app.setFont(QFont(load_ui_font(), 10))
    app.setStyleSheet(load_stylesheet())
    output.mkdir(parents=True, exist_ok=True)
    rows = []

    def pump(seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            app.processEvents()
            time.sleep(0.005)

    with TemporaryDirectory(prefix="scope-viewport-review-") as root:
        runtime = ApplicationRuntime(root=Path(root), hardware=False)
        key = runtime.add(ConnectionConfig("mock://scope-view", "lecroy.9404m", "mock"))
        runtime.connect(key).result(3)
        adapter = runtime.sessions.sessions[key].adapter
        adapter.values["1.timebase"] = {"value": 1e-6}
        for ch in (1, 2, 3, 4):
            adapter.values[f"{ch}.scale"] = {"value": (0.02, 0.05, 0.1, 0.2)[ch - 1]}
        acquire = adapter.acquire

        def fixture(request):
            time.sleep(0.03)
            batch = acquire(replace(request, points=5002))
            measurements = tuple(
                replace(
                    m,
                    axis=sample.axis if sample else np.linspace(-5e-6, 5e-6, 5002),
                    values=sample.values
                    if sample
                    else (
                        np.sin(np.linspace(0, 20, 5002) + m.channel * 0.3) * 0.7
                        + (2.4, 0.8, -0.8, -2.4)[m.channel - 1]
                    )
                    * adapter._value(m.channel, "scale"),
                    axis_unit="s",
                )
                for m in batch.measurements
            )
            return replace(
                batch,
                measurements=measurements,
                performance={"requested_points_is_not_a_hardware_limit": True},
            )

        adapter.acquire = fixture
        window = RealWindow(runtime)
        window.show()
        window.navigate("实时监测", "oscilloscope", key)
        page = window.stack.currentWidget()
        pump(2)
        assert len(page.controls.channels()) == 4
        page.start()
        try:
            pump(0.6)
            assert page.plot.displayed_frame is not None
            page.pin()
            for width, height in ((1280, 760), (1440, 900)):
                window.resize(width, height)
                for location in ("实时监测", "监测总览"):
                    window.navigate("实时监测", "oscilloscope" if location == "实时监测" else "", key)
                    pump(0.4)
                    name = f"{'page' if location == '实时监测' else 'overview'}-{width}x{height}-{scale}.png"
                    assert window.grab().save(str(output / name))
                    curves = page.plot.curves.values()
                    assert all(len(c.getData()[0]) > 1 for c in curves)
                    rows.append(
                        dict(
                            file=name,
                            logical_size=[width, height],
                            scale=scale,
                            raw_points=[len(m.values) for m in page.plot.displayed_frame.batch.measurements],
                            display_points=[len(c.getData()[0]) for c in curves],
                            ranges=page.plot.plot.viewRange(),
                            time_label=page.plot.plot.getAxis("bottom").label.toPlainText(),
                        )
                    )
            assert not (runtime.repository.root / "data").exists()
            if not sample:
                window.navigate("实时监测", "oscilloscope", key)
                page.record()
                pump(0.4)
                assert not page.controls.checks[1].isEnabled()
                window.grab().save(str(output / f"recording-locked-{scale}.png"))
                page.record()
                pump(0.5)
                runtime.monitoring.stop(key).result(5)
                page.controls.refresh_parameters()
                pump(1)
                page.controls.status.setText("等待当前配置的新帧 · 图中保留旧波形及刻度")
                window.grab().save(str(output / f"waiting-{scale}.png"))
        finally:
            deadline = time.monotonic() + 6
            while page.controls and page.controls.running and time.monotonic() < deadline:
                pump(0.05)
            assert not page.controls or not page.controls.running, "Preview control group did not drain"
            window.timer.stop()
            window.controller.close()
            runtime.close()
            window.closing = True
            window.close()
            window.bridge.worker.shutdown()
    (output / f"render-{scale}.json").write_text(
        json.dumps(
            {"hardware_access": False, "source": "archived_waveform" if sample else "mock", "renders": rows},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("design/reviews/oscilloscope"))
    parser.add_argument("--scale", choices=("1", "1.25", "1.5"), default="1")
    parser.add_argument("--waveform-file", type=Path)
    args = parser.parse_args()
    if args.waveform_file and args.output.resolve().is_relative_to(Path("design").resolve()):
        parser.error("Archived instrument data must be rendered into runtime or ignored evidence directories")
    render(args.output, args.scale, args.waveform_file)
