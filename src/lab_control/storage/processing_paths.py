"""Windows extended paths for nested offline caches; public paths stay ordinary."""

import os
from pathlib import Path


def filesystem_path(path: Path) -> Path:
    if os.name != "nt":
        return path
    value = os.path.abspath(path)
    if value.startswith("\\\\?\\"):
        return Path(value)
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


def public_path(path: Path) -> Path:
    value = str(path)
    if os.name == "nt":
        if value.startswith("\\\\?\\UNC\\"):
            return Path("\\\\" + value[8:])
        if value.startswith("\\\\?\\"):
            return Path(value[4:])
    return path


def readonly_uri(path: Path) -> str:
    path = filesystem_path(path)
    uri = path.as_uri()
    if os.name == "nt" and str(path).startswith("\\\\?\\"):
        # Path.as_uri puts '?' in the URI authority, which SQLite rejects.
        # Keep the extended prefix in the path with an empty authority instead.
        uri = uri.replace("file://", "file:////", 1)
    return uri + "?mode=ro&immutable=1"
