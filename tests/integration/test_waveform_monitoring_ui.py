from dataclasses import replace

import numpy as np
from PySide6.QtCore import Qt

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.ui.pages.monitoring.dashboard import MonitorCard
from lab_control.ui.real_window import RealWindow


def test_scope_page_and_overview_fit_current_frame_without_instrument_io(qtbot, tmp_path):
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    key = runtime.add(ConnectionConfig("mock://scope-ui", "lecroy.9404m", "mock"))
    runtime.connect(key).result(3)
    adapter = runtime.sessions.sessions[key].adapter
    acquire = adapter.acquire
    calls = []

    def short_trace(request):
        calls.append(request)
        batch = acquire(request)
        return replace(
            batch,
            measurements=tuple(
                replace(
                    m,
                    axis=np.linspace(-5e-6, 5e-6, len(m.values)),
                    axis_unit="s",
                )
                for m in batch.measurements
            ),
        )

    adapter.acquire = short_trace
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    window.navigate("实时监测", "oscilloscope", key)
    page = window.stack.currentWidget()
    try:
        page.start(once=True)
        qtbot.waitUntil(lambda: page.plot.displayed_frame is not None, timeout=5000)
        runtime.monitoring.stop(key).result(3)
        assert all(len(c.getData()[0]) > 1 for c in page.plot.curves.values())
        assert "0.5 ～ 60 s" in page.numeric_ranges.text()
        page.plot.plot.setXRange(-1, 1, padding=0)
        qtbot.mouseClick(page.fit_button, Qt.MouseButton.LeftButton)
        assert page.plot.plot.viewRange()[0][1] < 1e-5
        frame = page.plot.displayed_frame
        page.save()
        qtbot.waitUntil(
            lambda: bool(list(runtime.repository.root.glob("data/**/*/samples.npz"))), timeout=5000
        )
        with np.load(next(runtime.repository.root.glob("data/**/*/samples.npz"))) as saved:
            np.testing.assert_array_equal(saved["axis_0"], frame.batch.measurements[0].axis)
            np.testing.assert_array_equal(saved["values_0"], frame.batch.measurements[0].values)
        page.pin()
        window.navigate("实时监测")
        cards = window.findChildren(MonitorCard)
        qtbot.waitUntil(lambda: any(c.isVisible() and c.plot.displayed_frame for c in cards), timeout=5000)
        card = next(c for c in cards if c.isVisible())
        assert all(len(c.getData()[0]) > 1 for c in card.plot.curves.values())
        card.plot.plot.setXRange(-1, 1, padding=0)
        qtbot.mouseClick(card.fit_button, Qt.MouseButton.LeftButton)
        assert card.plot.plot.viewRange()[0][1] < 1e-5
        assert len(calls) == 1
    finally:
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.closing = True
        window.bridge.worker.shutdown()
