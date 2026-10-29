"""Run the explicit M300 LAN acceptance plan and preserve runtime evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lab_control.domain.instruments import jsonable, utc_now

STATUS_FIELDS = (
    "scan_list",
    "scan_size",
    "dmm_installed",
    "dmm_state",
    "trigger_source",
    "trigger_interval",
    "monitor_state",
    "slot_100",
    "slot_200",
    "slot_300",
    "slot_400",
    "slot_500",
    "errors",
)


class RecordingTransport:
    """Log the exact SCPI request and response while preserving the session owner."""

    def __init__(self, inner: Any, path: Path):
        self.inner, self.resource, self.path = inner, inner.resource, path

    def _write(self, value: bytes | str) -> None:
        raw = value if isinstance(value, bytes) else value.encode("ascii", "backslashreplace")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "time": utc_now(),
                        "direction": "tx",
                        "ascii": raw.decode("ascii", "backslashreplace"),
                        "hex": raw.hex(),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    def _read(self, raw: bytes) -> bytes:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "time": utc_now(),
                        "direction": "rx",
                        "ascii": raw.decode("ascii", "backslashreplace"),
                        "hex": raw.hex(),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        return raw

    def write(self, payload: bytes | str) -> None:
        self._write(payload)
        self.inner.write(payload)

    def read(self) -> bytes:
        return self._read(self.inner.read())

    def query(self, payload: bytes | str) -> bytes:
        self._write(payload)
        return self._read(self.inner.query(payload))

    def open(self) -> None:
        self.inner.open()

    def close(self) -> None:
        self.inner.close()


def save_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(jsonable(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    """Compatibility entry; the explicit thermistor plan owns control/recovery."""
    from .verify import main as verify_main

    return verify_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
