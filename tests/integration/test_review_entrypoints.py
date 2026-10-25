"""Exercise visual-review entrypoints against current navigation with isolated Mock data."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("script", ["review_parameter_typography.py", "review_power_panel.py"])
def test_visual_review_scripts_use_connected_production_pages(tmp_path, script):
    output = tmp_path / "screenshots"
    arguments = (
        [str(output), "--expect-dpr", "1"]
        if script == "review_parameter_typography.py"
        else ["--output", str(output)]
    )
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script), *arguments],
        cwd=ROOT,
        env={
            **os.environ,
            "QT_QPA_PLATFORM": "offscreen",
            "QT_SCALE_FACTOR": "1",
            "PYTHONIOENCODING": "utf-8",
            "LAB_CONTROL_HOME": str(tmp_path / "runtime"),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    if script == "review_parameter_typography.py":
        report = json.loads((output / "metrics.json").read_text(encoding="utf-8"))
        assert not report["hardware"] and report["parameter_writes"] == 0
        captures = report["captures"]
        assert len(captures) == 9
        assert {item["state"] for item in captures} == {"readback", "empty", "editing"}
        assert all(item["page"] == "SignalSourcePage" and item["fields"] for item in captures)
        names = [item["file"] for item in captures]
    else:
        captures = json.loads((output / "geometry.json").read_text(encoding="utf-8"))
        assert len(captures) == 6
        assert {item["model"] for item in captures} == {"gwinstek.gpd4303s", "itech.it6322"}
        assert all(item["page"] == "PowerMonitoringPage" for item in captures)
        assert all(item["horizontal_scroll"] == 0 for item in captures)
        names = [item["screenshot"] for item in captures]
    assert all(item["connected"] for item in captures)
    assert all((output / name).stat().st_size > 0 for name in names)
