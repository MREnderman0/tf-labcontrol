from __future__ import annotations

import ctypes as ct
import os
import struct
from pathlib import Path
from typing import Any

from lab_control.domain.instruments import DeviceError


class VendorLibrary:
    """Typed DLL calls; no write/query emulation and no import-time driver load."""

    def __init__(self, path: str, library: Any = None) -> None:
        self.directory: Any = None
        if library is not None:
            self.library = library
            return
        if not path or not Path(path).is_absolute() or not Path(path).is_file():
            raise DeviceError("Configure an absolute SDK DLL path", "driver_missing")
        if os.name != "nt":
            raise DeviceError("This DLL backend requires Windows", "driver_missing")
        try:
            self.directory = os.add_dll_directory(str(Path(path).parent))
            self.library = ct.WinDLL(path)
        except OSError as exc:
            if self.directory:
                self.directory.close()
            raise DeviceError(
                f"SDK load failed ({struct.calcsize('P') * 8}-bit Python): {exc}", "sdk_load_failed"
            ) from exc

    def function(self, name: str, args: list[Any], result: Any = ct.c_int32):
        try:
            function = getattr(self.library, name)
        except AttributeError as exc:
            raise DeviceError(f"SDK symbol missing: {name}", "sdk_version") from exc
        function.argtypes, function.restype = args, result
        return function

    def close(self) -> None:
        if self.directory:
            self.directory.close()
            self.directory = None
