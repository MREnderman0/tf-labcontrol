"""Declarative component inventory. Never executes installation instructions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


def manifest_path() -> Path:
    for path in (
        Path(__file__).with_name("components.json"),
        Path(__file__).resolve().parents[3] / "deployment/components.json",
        Path(sys.prefix) / "lab_control_deployment/components.json",
    ):
        if path.is_file():
            return path
    raise FileNotFoundError("Deployment component inventory is missing")


def load_manifest(path: Path | None = None) -> dict[str, Any]:
    value = json.loads((path or manifest_path()).read_text(encoding="utf-8-sig"))
    if value.get("schema_version") != 1 or not isinstance(value.get("components"), list):
        raise ValueError("Unsupported component inventory")
    errors = validate_manifest(value)
    if errors:
        raise ValueError("; ".join(errors))
    return value


def validate_manifest(value: dict) -> list[str]:
    errors = []
    seen = set()
    required = {
        "id",
        "name",
        "purpose",
        "transports",
        "adapters",
        "sdk",
        "vendor",
        "version",
        "architecture",
        "systems",
        "source_page",
        "url",
        "filename",
        "sha256",
        "signature",
        "redistribution",
        "detection",
        "installation",
        "recovery",
        "validation",
    }
    for item in value.get("components", []):
        key = item.get("id", "<missing>")
        if required - item.keys():
            errors.append(f"{key}: missing fields {sorted(required - item.keys())}")
        if not re.fullmatch(r"[a-z][a-z0-9-]+", key) or key in seen:
            errors.append(f"{key}: invalid or duplicate ID")
        seen.add(key)
        filename = item.get("filename")
        if filename and (Path(filename).name != filename or re.search(r"[\\/:]", filename)):
            errors.append(f"{key}: filename must be a cache basename")
        digest = item.get("sha256")
        if digest and not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            errors.append(f"{key}: invalid SHA256")
        if item.get("architecture") != "x64":
            errors.append(f"{key}: target architecture must be x64")
        for field in ("signature", "redistribution", "detection", "installation", "validation"):
            if not isinstance(item.get(field), dict):
                errors.append(f"{key}: {field} must be an object")
    return errors


def cache_directories() -> tuple[Path, ...]:
    folders = [Path(os.environ.get("PROGRAMDATA", Path.home())) / "TF-LabControl/maintenance/packages"]
    if not getattr(sys, "frozen", False):
        folders.append(Path(__file__).resolve().parents[3] / ".tools/installers")
    else:
        folders.append(Path(sys.executable).parent / "maintenance/packages")
    return tuple(folders)


def payload_status(component: dict, caches: tuple[Path, ...], cancel=None) -> dict:
    filename, digest = component["filename"], component["sha256"]
    if not filename or not digest:
        return {"state": "unknown", "reason": "安装材料尚未登记完整"}
    failures = []
    for folder in caches:
        path = folder / filename
        if not path.is_file():
            continue
        try:
            sha = hashlib.sha256()
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    if cancel and cancel.is_set():
                        return {"state": "cancelled", "reason": "检查已取消"}
                    sha.update(block)
            if sha.hexdigest().lower() == digest.lower():
                return {"state": "verified", "path": str(path), "sha256": sha.hexdigest()}
            failures.append(str(path))
        except OSError as exc:
            failures.append(str(exc))
    return {
        "state": "invalid" if failures else "missing",
        "reason": "; ".join(failures) or "未找到离线安装材料",
    }


def release_issues(component: dict, caches: tuple[Path, ...]) -> list[str]:
    issues = []
    for field in ("version", "source_page", "url", "filename", "sha256"):
        if not component[field]:
            issues.append(f"{field} pending")
    for field in ("redistribution", "installation", "validation"):
        if component[field].get("state") != "verified" or not component[field].get("evidence"):
            issues.append(f"{field} not verified")
    install = component["installation"]
    if not isinstance(install.get("arguments"), list) or not install.get("success_codes"):
        issues.append("installation arguments/exit codes pending")
    if not isinstance(install.get("reboot_codes"), list):
        issues.append("reboot exit codes pending")
    if component["detection"].get("kind") == "pending":
        issues.append("installed-component detection pending")
    signature = component["signature"]
    if signature.get("policy") != "authenticode" or not signature.get("publisher"):
        issues.append("signature policy/publisher pending")
    payload = payload_status(component, caches)
    if payload["state"] != "verified":
        issues.append("offline payload " + payload["state"])
    elif not signature.get("member") and signature.get("publisher"):
        from lab_control.deployment.windows import signature_status

        result = signature_status(Path(payload["path"]))
        if (
            result.get("status") != "Valid"
            or signature["publisher"].casefold() not in result.get("subject", "").casefold()
        ):
            issues.append("installer signature could not be verified")
    elif signature.get("member"):
        # Archive signing needs extraction/verification in the future release stager.
        issues.append("archive member signature requires release staging verification")
    return issues


def required_components(configs, *, lan: bool = False, dhcp: bool = False) -> set[str]:
    from lab_control.instruments.devices.catalog import load_definitions

    definitions = load_definitions()
    manifest = load_manifest()
    required = {"msvc-runtime"}
    if lan:
        required.add("rs-visa")
    if dhcp:
        required.add("tftpd64")
    for config in configs:
        if config.transport == "mock":
            continue
        for component in manifest["components"]:
            if config.transport in component["transports"] or config.adapter in component["adapters"]:
                required.add(component["id"])
        definition = definitions.get(config.adapter)
        if definition:
            by_id = {c["id"]: c for c in manifest["components"]}
            required.update(
                key
                for key in definition.deployment_components
                if (not by_id[key]["transports"] or config.transport in by_id[key]["transports"])
            )
        if config.resource.upper().startswith("GPIB"):
            required.add("gpib-interface")
        if (config.transport == "serial" or config.resource.upper().startswith("ASRL")) and not (
            config.adapter == "gwinstek.gpd4303s" or config.adapter == "keithley.2231a"
        ):
            required.add("serial-interface")
    return required
