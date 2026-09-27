import pytest
from PySide6.QtWidgets import QFrame, QVBoxLayout

from lab_control.app import load_stylesheet
from lab_control.ui.pages.monitoring.recording_state import RecordingButton, update_recording_frame


@pytest.mark.parametrize("name", ["spectrumChart", "monitorCard"])
def test_recording_frame_pixels_and_geometry_follow_binary_drain(qtbot, name):
    frame = QFrame()
    frame.setObjectName(name)
    frame.setStyleSheet(load_stylesheet())
    layout = QVBoxLayout(frame)
    button = RecordingButton(frame)
    layout.addWidget(button)
    qtbot.addWidget(frame)
    frame.resize(300, 180)
    frame.show()
    phases = [
        ({}, "idle", "开始记录", False),
        ({"running": True, "recording": True}, "recording", "停止记录", True),
        ({"running": True, "recording_state": {"binary_done": False}}, "draining", "收尾中…", False),
        (
            {"running": True, "recording_state": {"binary_done": True, "converted": 0}},
            "idle",
            "开始记录",
            True,
        ),
        ({"error": "write failed", "recording_state": {"binary_done": True}}, "idle", "开始记录", False),
    ]
    rect = None
    for task, phase, text, enabled in phases:
        update_recording_frame(frame, task)
        button.refresh(task, connected=True, busy=False, locked=phase != "idle")
        qtbot.wait(10)
        rect = rect or frame.contentsRect()
        assert frame.contentsRect() == rect
        assert frame.property("recordingPhase") == phase
        assert button.text() == text and button.isEnabled() == enabled
        pixels = frame.grab().toImage()
        color = pixels.pixelColor(pixels.width() // 2, 0).name()
        assert (color == "#b96360") == (phase != "idle")
        assert frame.toolTip() == "" if phase == "idle" else "本设备" in frame.toolTip()
    button.refresh({"running": True}, connected=True, busy=True, locked=False)
    assert not button.isEnabled()
    button.refresh({"running": True}, connected=True, busy=False, locked=True)
    assert not button.isEnabled()
