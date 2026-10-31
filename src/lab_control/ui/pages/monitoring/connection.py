"""Shared manual recovery presentation for each monitoring workspace."""

from PySide6.QtWidgets import QPushButton

from lab_control.ui.controller import WorkspaceController
from lab_control.ui.pages.monitoring.recording_state import recording_phase


def refresh_reconnect_button(
    button: QPushButton,
    controller: WorkspaceController,
    device_id: str,
    task: dict,
    *,
    busy: bool = False,
) -> None:
    """Override only the idle failed action; an active monitor must remain stoppable."""
    state = controller.states.get(device_id, {})
    if state.get("activeTask") == "reconnect":
        button.setText("重连中…")
        button.setEnabled(False)
        button.setToolTip("正在重新连接并核验当前设备身份")
    elif state.get("connectionState") in {"degraded", "error"} and not task.get("running"):
        button.setText("重连")
        button.setEnabled(
            bool(state.get("identity"))
            and not busy
            and not any(
                state.get(key) for key in ("pending", "controlPending", "controlGroup", "controlsLocked")
            )
            and not task.get("pending")
            and recording_phase(task) == "idle"
        )
        button.setToolTip(
            (str(state.get("lastError") or "设备连接异常") + "\n")
            + "重新连接当前设备；成功后可手动开始监测。"
        )
    else:
        button.setToolTip("")
