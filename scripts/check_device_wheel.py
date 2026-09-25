"""Check shipped device modules outside the source tree, without opening hardware."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

SMOKE = r"""
import importlib
import json
import socket
import sys
from pathlib import Path

import pyvisa

def forbidden(*args, **kwargs):
    raise AssertionError("Metadata inspection attempted hardware access")

pyvisa.ResourceManager = forbidden
socket.create_connection = forbidden
root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
import lab_control
from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import Unsupported
from lab_control.instruments import registry
from lab_control.transport.base import MockTransport

assert Path(lab_control.__file__).resolve().is_relative_to(root)
registry.transport_for = forbidden
for definition in registry.DEVICE_DEFINITIONS.values():
    config = ConnectionConfig("metadata://test", definition.adapter_id)
    assert registry.describe(config).operations
    transport = MockTransport([])
    assert type(registry.build_scpi_adapter(config, transport)) is definition.adapter_type
    assert not transport.history
    for file in ("README.md", "hardware/README.md", "hardware/records.py", "tests/fixtures/control.json", "tests/fixtures/capabilities-before.json"):
        assert (definition.root / file).is_file(), file
    importlib.import_module(definition.adapter_type.__module__.rsplit(".", 1)[0] + ".hardware.readonly")
    records = importlib.import_module(definition.adapter_type.__module__.rsplit(".", 1)[0] + ".hardware.records")
    from lab_control.storage.hardware_records import validate
    summary = records.summarize({"stages": {"physical_output": "not_tested"}})
    assert summary["adapter_id"] == definition.adapter_id
    assert not summary["identity_verified"] and not validate(summary)
    if (definition.root / "hardware/validate_usb.py").exists():
        verification = importlib.import_module(definition.adapter_type.__module__.rsplit(".", 1)[0] + ".hardware.validate_usb")
        evidence = verification.capture_sources(root / "evidence")
        assert evidence["source_sha256"]
        assert evidence["project_head"].startswith("Installed package")
removed = sys.argv[2]
if removed:
    assert removed not in registry.PROFILES
    try:
        registry.create_adapter(ConnectionConfig("USB::UNAVAILABLE", removed), None)
    except Unsupported:
        pass
    else:
        raise AssertionError("Removed adapter was accepted")
print(json.dumps({"modules": {key: d.root.name for key, d in registry.DEVICE_DEFINITIONS.items()}, "removed": removed, "isolated_import": True}))
"""


def smoke(root: Path, removed: str = "") -> dict:
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", SMOKE, str(root), removed],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(completed.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="lab-device-wheel-") as temporary:
        root = Path(temporary).resolve()
        with zipfile.ZipFile(args.wheel) as archive:
            for name in archive.namelist():
                if not (root / name).resolve().is_relative_to(root):
                    raise ValueError("Unsafe wheel member")
            archive.extractall(root)
        baseline = smoke(root)
        print(json.dumps(baseline))
        catalog = root / "lab_control/instruments/devices/catalog.py"
        source = catalog.read_text(encoding="utf-8")
        for adapter_id in baseline["modules"]:
            # The explicit catalog is the sole source for module paths.
            package_root = root / "lab_control/instruments/devices"
            match = package_root / baseline["modules"][adapter_id]
            module = "lab_control.instruments.devices." + match.name
            catalog.write_text(source.replace(f'    "{module}",\n', ""), encoding="utf-8")
            assert module not in catalog.read_text(encoding="utf-8")
            parked = root / (match.name + "-unregistered")
            assert match.resolve().is_relative_to(root) and parked.resolve().is_relative_to(root)
            shutil.move(str(match), str(parked))
            try:
                result = smoke(root, adapter_id)
                assert len(result["modules"]) == len(baseline["modules"]) - 1
                print(json.dumps(result))
            finally:
                shutil.move(str(parked), str(match))
                catalog.write_text(source, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
