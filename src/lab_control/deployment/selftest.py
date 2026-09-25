"""Explicit offline smoke test for the actual frozen executable."""

from __future__ import annotations

import importlib.metadata
import os
import sys
import tempfile
import traceback
from pathlib import Path


def run(destination: Path) -> int:
    from lab_control.deployment.environment import export_report

    result = {
        "schema_version": 1,
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "hardware_accessed": False,
        "passed": False,
    }
    try:
        import numpy as np
        import pyvisa

        def forbidden(*args, **kwargs):
            raise AssertionError("Deployment smoke attempted hardware access")

        setattr(pyvisa, "ResourceManager", forbidden)
        from lab_control.deployment.catalog import load_manifest
        from lab_control.instruments.devices.catalog import load_definitions

        result["packages"] = {
            name: importlib.metadata.version(name) for name in load_manifest()["python_roots"]
        }
        result["models"] = list(load_definitions())
        from lab_control.domain.instruments import DeviceIdentity, Measurement, MeasurementBatch
        from lab_control.storage.recording import Recording, read_frames

        with tempfile.TemporaryDirectory(prefix="labcontrol-中文 smoke-") as directory:
            root = Path(directory)
            os.environ["LAB_CONTROL_HOME"] = str(root)
            os.environ["LOCALAPPDATA"] = str(root / "local")
            batch = MeasurementBatch(
                DeviceIdentity("Fixture", "Deployment"),
                (Measurement("test", "V", np.arange(8, dtype="<f4")),),
                {},
            )
            recording = Recording(root, "electronic")
            assert recording.reserve(8192)
            assert recording.submit(batch)
            paths = recording.finish().result(30)
            assert recording.snapshot()["converted"] == 1, recording.snapshot()
            assert len(list(read_frames(Path(paths[0])))) == 1
            assert Path(paths[0]).with_suffix(".csv").is_file()
            result["recording"] = {"committed": 1, "converted": 1}
            from PySide6.QtGui import QFont
            from PySide6.QtWidgets import QApplication

            from lab_control.app import load_stylesheet, load_ui_font
            from lab_control.application.runtime import ApplicationRuntime
            from lab_control.ui.real_window import RealWindow

            app = QApplication.instance() or QApplication([])
            assert isinstance(app, QApplication)
            app.setStyleSheet(load_stylesheet())
            font = load_ui_font()
            result["font"] = font
            app.setFont(QFont(font, 10))
            runtime = ApplicationRuntime(root=root, hardware=False)
            window = RealWindow(runtime)
            window.navigate("系统设置")
            window.show()
            app.processEvents()
            destination.parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(destination.with_suffix(".png")))
            window.environment_panel.shutdown()
            window.controller.close()
            runtime.close()
            window.closing = True
            window.close()
            window.bridge.worker.shutdown(wait=True)
            assert not list(root.rglob("hardware.lock"))
            result["gui"] = "settings rendered without hardware"
        result["passed"] = True
    except Exception:
        result["error"] = traceback.format_exc()
    export_report(result, destination)
    return 0 if result["passed"] else 1
