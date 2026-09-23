"""Read-only dependency diagnostics. Loading a DLL never opens an instrument."""

from __future__ import annotations

import importlib.metadata
import os
import struct
from pathlib import Path
from typing import Any

from lab_control.application.config import ConnectionConfig
from lab_control.transport.sdk import VendorLibrary

SDK_LIBRARIES = {
    "pico": ("ps6000a.dll", ("ps6000aOpenUnit", "ps6000aCloseUnit", "ps6000aEnumerateUnits")),
    "tlpmx": ("TLPMX_64.dll", ("TLPMX_init", "TLPMX_close", "TLPMX_findRsrc")),
    "pm100d": ("PM100D_64.dll", ("PM100D_init", "PM100D_close")),
}


def sdk_candidates(kind: str) -> tuple[Path, ...]:
    filename = SDK_LIBRARIES[kind][0]
    roots = [
        Path(os.environ.get(key, default))
        for key, default in (
            ("ProgramFiles", r"C:\Program Files"),
            ("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        )
    ]
    result: list[Path] = []
    for root in roots:
        for folder in (
            root / "Pico Technology" / "SDK",
            root / "Thorlabs" / "PowerMeters",
            root / "IVI Foundation" / "VISA" / "Win64",
        ):
            if folder.is_dir():
                result.extend(folder.rglob(filename))
    return tuple(sorted(set(result)))


def pe_bits(path: Path) -> int:
    with path.open("rb") as file:
        if file.read(2) != b"MZ":
            raise ValueError("Not a Windows PE library")
        file.seek(0x3C)
        offset = struct.unpack("<I", file.read(4))[0]
        file.seek(offset)
        if file.read(4) != b"PE\0\0":
            raise ValueError("Invalid PE header")
        machine = struct.unpack("<H", file.read(2))[0]
    return {0x8664: 64, 0x14C: 32}.get(machine, 0)


def find_sdk(kind: str) -> str:
    for path in sdk_candidates(kind):
        try:
            if pe_bits(path) == struct.calcsize("P") * 8:
                return str(path)
        except (ValueError, OSError, struct.error):
            continue
    return ""


def dependency_diagnostics(configs: tuple[ConnectionConfig, ...]) -> dict[str, Any]:
    packages = {}
    for name in ("PySide6", "PyVISA", "pyserial", "numpy", "pyqtgraph", "pytest", "ruff", "mypy", "uv"):
        try:
            packages[name] = {"state": "installed", "version": importlib.metadata.version(name)}
        except importlib.metadata.PackageNotFoundError:
            packages[name] = {"state": "missing"}
    libraries: dict[str, Any] = {}
    for kind, (_, symbols) in SDK_LIBRARIES.items():
        paths = {c.sdk_path for c in configs if c.transport == kind and c.sdk_path}
        paths.update(str(path) for path in sdk_candidates(kind))
        checks = []
        for value in sorted(paths):
            check: dict[str, Any] = {"path": value, "state": "missing", "hardware_accepted": False}
            library = None
            try:
                check["bits"] = pe_bits(Path(value))
                if check["bits"] != struct.calcsize("P") * 8:
                    raise ValueError("DLL bitness does not match Python")
                library = VendorLibrary(value)
                for symbol in symbols:
                    if not hasattr(library.library, symbol):
                        raise ValueError("Missing SDK symbol: " + symbol)
                check.update(state="loadable", symbols=list(symbols))
            except Exception as exc:
                check.update(state="error" if Path(value).exists() else "missing", error=str(exc))
            finally:
                if library:
                    library.close()
            checks.append(check)
        libraries[kind] = {
            "state": "loadable"
            if any(c["state"] == "loadable" for c in checks)
            else "error"
            if checks
            else "missing",
            "libraries": checks,
        }
    return {
        "packages": packages,
        "sdk": libraries,
        "note": "Dependency availability only; no instrument was opened or accepted.",
    }
