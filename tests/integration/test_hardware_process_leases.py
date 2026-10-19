"""OS locks are exercised across processes; no instrument or VISA I/O."""

import json
import os
import subprocess
import sys

import pytest

from lab_control.application.leases import ProcessLease, ResourceLeases
from lab_control.domain.instruments import DeviceError


def child(root, resources=(), *, identified=True, shared=True):
    code = """
import json, sys
from pathlib import Path
from lab_control.application.leases import ProcessLease, ResourceLeases
from lab_control.domain.instruments import DeviceError
root, resources, identified, shared = json.loads(sys.argv[1])
gate = None
leases = ResourceLeases(Path(root))
try:
    gate = ProcessLease(Path(root), shared=shared)
    if resources:
        leases.acquire('child', tuple(resources), identified=identified)
    print('acquired')
except DeviceError as exc:
    print(exc.code)
finally:
    leases.release('child')
    if gate:
        gate.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", code, json.dumps([str(root), resources, identified, shared])],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    return result.stdout.strip()


def test_distinct_assets_parallel_same_asset_and_endpoint_excluded(tmp_path):
    gate = ProcessLease(tmp_path, shared=True)
    leases = ResourceLeases(tmp_path)
    leases.acquire("scope", ("TCPIP0::192.0.2.10::inst0::INSTR", "asset:scope:TEST-1"), identified=True)
    try:
        assert child(tmp_path, ("TCPIP0::192.0.2.11::inst0::INSTR", "asset:source:TEST-2")) == "acquired"
        for resource in ("vicp://192.0.2.10:1861", "TCPIP::192.0.2.10::5025::SOCKET", "asset:scope:TEST-1"):
            assert child(tmp_path, (resource,)) == "resource_busy"
        assert child(tmp_path, ("USB::UNKNOWN",), identified=False) == "resource_busy"
        assert child(tmp_path, shared=False) == "process_busy"
    finally:
        leases.release("scope")
        gate.close()
    assert child(tmp_path, ("asset:scope:TEST-1",)) == "acquired"


def test_unknown_probe_excludes_known_sessions_and_failed_claim_rolls_back(tmp_path):
    leases = ResourceLeases(tmp_path)
    leases.acquire("probe", ("USB::UNKNOWN", "discovery:unknown_alias"))
    assert child(tmp_path, ("asset:scope:TEST-1",)) == "resource_busy"
    leases.release("probe")
    leases.acquire("source", ("resource-z",), identified=True)
    other = ResourceLeases(tmp_path)
    with pytest.raises(DeviceError):
        other.acquire("failed", ("resource-a", "resource-z"), identified=True)
    assert child(tmp_path, ("resource-a",)) == "acquired"
    leases.release("source")


@pytest.mark.skipif(os.name != "nt", reason="Windows legacy byte lock interoperability")
def test_existing_msvcrt_process_lock_is_respected(tmp_path):
    import msvcrt

    with (tmp_path / "hardware.lock").open("w+b") as stream:
        stream.write(b"0")
        stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            assert child(tmp_path) == "process_busy"
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    assert child(tmp_path) == "acquired"


def test_process_exit_releases_claim(tmp_path):
    code = """
import os, sys
from pathlib import Path
from lab_control.application.leases import ProcessLease, ResourceLeases
root = Path(sys.argv[1])
gate = ProcessLease(root, shared=True)
leases = ResourceLeases(root)
leases.acquire('exit', ('asset:scope:TEST-1',), identified=True)
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", code, str(tmp_path)], check=True, timeout=15)
    assert child(tmp_path, ("asset:scope:TEST-1",)) == "acquired"


def test_exclusive_process_lease_reports_holder_and_cleans_advisory_metadata(tmp_path):
    first = ProcessLease(tmp_path)
    metadata = tmp_path / "hardware.lock.owner.json"
    try:
        assert metadata.exists()
        with pytest.raises(DeviceError, match=r"PID \d+"):
            ProcessLease(tmp_path)
    finally:
        first.close()
    assert not metadata.exists()


def test_process_lease_cleans_stale_metadata_from_redirected_appdata_view(tmp_path, monkeypatch):
    physical = tmp_path / "physical" / "TF-LabControl"
    logical = tmp_path / "logical" / "TF-LabControl"
    physical.mkdir(parents=True)
    logical.mkdir(parents=True)
    physical_lock = physical / "hardware.lock"
    logical_lock = logical / "hardware.lock"
    physical_lock.touch()
    os.link(physical_lock, logical_lock)
    stale = logical / "hardware.lock.owner.json"
    stale.write_text('{"pid": 123}', encoding="utf-8")
    monkeypatch.setenv("LAB_CONTROL_HOME", str(logical))

    lease = ProcessLease(physical)
    try:
        assert not stale.exists()
    finally:
        lease.close()
