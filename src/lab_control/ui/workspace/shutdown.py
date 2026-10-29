from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lab_control.ui.real_window import RealWindow


class WindowShutdown:
    """One-shot asynchronous workspace shutdown."""

    def __init__(self, window: RealWindow) -> None:
        self.window = window

    def closeEvent(self, event) -> None:  # noqa: N802
        window = self.window
        if window.closing:
            event.accept()
            return
        event.ignore()
        if window._shutdown_started:
            return
        window._shutdown_started = True
        window.timer.stop()
        window.agent_panel.timer.stop()
        window.controller.close()
        window.message.setText("正在停止扫描和采集并释放本软件会话…")
        central = window.centralWidget()
        if central:
            central.setEnabled(False)

        def cleanup():
            errors = []
            for close in (
                window.agent_panel.shutdown,
                window.storage_panel.shutdown,
                window.environment_panel.shutdown,
                window.runtime.close,
            ):
                try:
                    close()
                except Exception as exc:
                    errors.append(exc)
            if errors:
                raise ExceptionGroup("关闭期间发生错误：" + "; ".join(map(str, errors)), errors)

        future = window.bridge.worker.submit(cleanup)

        def finish(label: str, result: Any):
            if label == "shutdown":
                window.closing = True
                window.bridge.worker.shutdown(wait=False)
                window.close()

        window.bridge.completed.connect(finish)
        window.bridge.watch(future, "shutdown")
