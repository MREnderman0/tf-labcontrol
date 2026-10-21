# Reproducible Windows x64 directory build. No vendor DLLs are copied from the host.
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
datas = collect_data_files("lab_control", includes=["ui/styles/*.qss", "instruments/devices/**/*.md", "instruments/devices/**/tests/fixtures/*.json"])
datas += [(str(root / "deployment/components.json"), "lab_control/deployment")]
datas += [(os.environ.get("LABCONTROL_BUILD_MANIFEST", str(root / "build/deployment/release-manifest.json")), "lab_control/deployment")]
for distribution in ("PySide6", "PyVISA", "pyserial", "numpy", "pyqtgraph", "shiboken6", "PySide6_Essentials", "PySide6_Addons"):
    datas += copy_metadata(distribution)
hidden = collect_submodules("lab_control", filter=lambda name: ".tests" not in name and ".hardware" not in name)
a = Analysis([str(root / "deployment/entry.py")], pathex=[str(root / "src")], datas=datas,
             hiddenimports=hidden, excludes=["pytest", "ruff", "mypy", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets"])
# Reject accidental dependencies supplied by other software on the build machine.
allowed = tuple(Path(p).resolve() for p in (sys.prefix, sys.base_prefix, os.environ['WINDIR']))
for destination, source, kind in a.binaries:
    if not any(Path(source).resolve().is_relative_to(folder) for folder in allowed):
        raise ValueError(f"Unregistered native build dependency: {source}")
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="TF-LabControl", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="TF-LabControl")
