from dataclasses import replace

import numpy as np
import pytest
from PySide6.QtCore import QPointF

from lab_control.domain.oscilloscope import ScopeDisplay

from .test_oscilloscope_grid import SOURCE, frame
from .test_oscilloscope_grid import plot as plot


def signed_frame(version=1, sign=-1, scale=None):
    original = frame(version=version)
    measurements = tuple(
        replace(
            m,
            metadata={
                "scope_display": replace(
                    ScopeDisplay.parse(m.metadata["scope_display"]),
                    vertical_offset_sign=-1,
                    horizontal_position_sign=sign,
                    units_per_div=scale or 0.01 * m.channel,
                ).metadata()
            },
        )
        for m in original.batch.measurements
    )
    return replace(original, batch=replace(original.batch, measurements=measurements))


@pytest.mark.parametrize("sign", [-1, 1])
def test_horizontal_vertical_preview_changes_scene_but_preserves_saved_frame(plot, qtbot, sign):
    sample = signed_frame(sign=sign)
    plot.show_frame(sample, SOURCE, (1, 2, 3, 4))
    saved = plot.displayed_frame
    arrays = {k: (c.xData, c.yData) for k, c in plot.curves.items()}
    plot.preview_parameter(1, "scale", 0.02)
    plot.preview_parameter(2, "offset", 0.01)
    plot.preview_parameter(1, "timebase", 2e-6)
    plot.preview_parameter(1, "time_position", 1e-6)
    qtbot.waitUntil(lambda: "估算" in plot.meta.text())
    assert plot.grid_range == pytest.approx((sign * 1e-6 - 10e-6, sign * 1e-6 + 10e-6))
    assert plot.plot.viewRange()[1] == [-4, 4] and len(plot.grid_lines) == 20
    assert plot.curves[1, "waveform"].transform().map(QPointF(0, 1)).y() == 0.5
    assert plot.curves[2, "waveform"].transform().map(QPointF(0, 1)).y() == 1.5
    assert plot.curves[3, "waveform"].transform().isIdentity()
    assert plot.displayed_frame is saved and "保存仍用原始数据" in plot.meta.text()
    for key, curve in plot.curves.items():
        assert curve.xData is arrays[key][0] and curve.yData is arrays[key][1]
        assert len(curve.getData()[0]) > 0 and not curve.curve.getPath().isEmpty()
    for before, after in zip(sample.batch.measurements, saved.batch.measurements, strict=True):
        np.testing.assert_array_equal(before.values, after.values)
        assert before.metadata == after.metadata
    plot.clear_preview()
    assert plot.grid_range == pytest.approx((-5e-6, 5e-6))
    assert all(c.transform().isIdentity() for c in plot.curves.values())
    assert plot.displayed_frame is saved and "估算" not in plot.meta.text()


def test_preview_waits_for_valid_committed_version_and_uses_actual_readback(plot, qtbot):
    old = signed_frame()
    plot.show_frame(old, SOURCE, (1,))
    plot.preview_parameter(1, "scale", 0.02)
    qtbot.waitUntil(lambda: "估算" in plot.meta.text())
    plot.commit_preview(2)
    plot.show_frame(replace(old, sequence=2), SOURCE, (1,))
    assert "等待实测" in plot.meta.text()
    before = plot.displayed_frame
    bad = replace(
        signed_frame(2),
        batch=replace(
            signed_frame(2).batch,
            measurements=(replace(old.batch.measurements[0], values=np.array([np.nan])),),
        ),
    )
    plot.show_frame(bad, SOURCE, (1,))
    assert plot.displayed_frame is before and plot.preview_targets
    plot.show_frame(signed_frame(2, scale=0.03), SOURCE, (1,))
    assert not plot.preview_targets and "估算" not in plot.meta.text()
    assert plot.curves[1, "waveform"].transform().isIdentity()
    assert np.max(plot.curves[1, "waveform"].yData) == pytest.approx(1 / 3, abs=1e-5)


def test_next_gesture_is_not_erased_by_previous_commands_frame(plot, qtbot):
    plot.show_frame(signed_frame(), SOURCE, (1, 2))
    plot.preview_parameter(1, "scale", 0.02)
    plot.commit_preview(2)
    plot.preview_parameter(2, "offset", 0.01)
    plot.show_frame(signed_frame(2), SOURCE, (1, 2))
    assert (1, "scale") not in plot.preview_targets and (2, "offset") in plot.preview_targets
    assert "拖动预览" in plot.meta.text()
    plot.reset()
    assert not plot.preview_targets
