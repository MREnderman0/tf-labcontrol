"""Verify OS-resolved runtime paths before real hardware I/O."""

from __future__ import annotations

from pathlib import Path

from lab_control.domain.instruments import DeviceError
from lab_control.storage.runtime import physical_path


def verify_runtime_paths(home: Path, lock_root: Path) -> dict[str, str]:
    paths = {
        "runtime": home,
        "inventory": home / "inventory.sqlite3",
        "global_lock": lock_root / "hardware.lock",
        "device_locks": lock_root / "devices",
    }
    if (home / "config").exists():
        paths["config"] = home / "config"
    result = {name: str(physical_path(path)) for name, path in paths.items()}
    if any(Path(result[name]) != path.absolute() for name, path in paths.items()):
        raise DeviceError(f"Formal runtime paths are redirected: {result}", "path_redirected")
    return result
