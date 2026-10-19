from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication


def load_stylesheet() -> str:
    stylesheet_path = Path(__file__).parent / "ui" / "styles" / "card_workspace.qss"
    return stylesheet_path.read_text(encoding="utf-8")


def load_ui_font() -> str:
    """Load a Windows CJK font when Qt does not expose system fonts directly."""
    candidates = (
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts/msyh.ttc",
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts/NotoSansSC-VF.ttf",
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts/simsun.ttc",
    )
    for font_path in candidates:
        if font_path.exists():
            font_id = QFontDatabase.addApplicationFont(str(font_path))
            if font_id >= 0:
                families = QFontDatabase.applicationFontFamilies(font_id)
                if families:
                    return families[0]
    return "Microsoft YaHei UI"


def main() -> int:
    import argparse

    from PySide6.QtWidgets import QMessageBox

    from lab_control.application.config import load_startup_config
    from lab_control.application.runtime import ApplicationRuntime
    from lab_control.ui.real_window import RealWindow

    parser = argparse.ArgumentParser(description="TF LabControl real instrument overview")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    app = QApplication(sys.argv)
    app.setApplicationName("TF LabControl")
    app.setFont(QFont(load_ui_font(), 10))
    app.setStyle("Fusion")
    app.setStyleSheet(load_stylesheet())
    try:
        configurations = load_startup_config(args.config)
        runtime = ApplicationRuntime(configurations)
    except Exception as exc:
        QMessageBox.critical(None, "启动失败", str(exc))
        return 2
    window = RealWindow(runtime)
    window.show()
    # Every transport is scanned once after the window is shown.  LAN discovery
    # is optional, but serial/VISA discovery must not depend on that setting.
    QTimer.singleShot(0, window.scan)
    result = app.exec()
    runtime.close()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
