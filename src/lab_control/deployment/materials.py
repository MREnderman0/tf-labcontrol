"""Explicit, bounded component preparation. Never install or access instruments."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from lab_control.deployment.catalog import cache_directories, load_manifest, payload_status, user_cache


def safe_relative(value: str) -> Path:
    """Reject Windows aliases, ADS, traversal and archive symlinks' target paths."""
    path = PurePosixPath(value.replace("\\", "/"))
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if (
        not value
        or path.is_absolute()
        or any(
            p in {".", ".."} or ":" in p or p.endswith((".", " ")) or p.split(".")[0].upper() in reserved
            for p in value.replace("\\", "/").split("/")
        )
    ):
        raise ValueError("Unsafe material path: " + value)
    return Path(*path.parts)


def sources(component: dict) -> list[dict]:
    return component.get("download_sources") or (
        [{"url": component["url"], "kind": "official-direct"}] if component.get("url") else []
    )


def component_closure(ids, manifest=None) -> list[dict]:
    catalog = {c["id"]: c for c in (manifest or load_manifest())["components"]}
    result, done, visiting = [], set(), set()

    def visit(key):
        if key in done:
            return
        if key not in catalog or key in visiting:
            raise ValueError("Unknown or cyclic component dependency: " + key)
        visiting.add(key)
        for dependency in catalog[key].get("dependencies", []):
            visit(dependency)
        visiting.remove(key)
        done.add(key)
        result.append(catalog[key])

    for key in ids:
        visit(key)
    return result


def _cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise InterruptedError("准备已取消")


def check_file(path: Path, expected: dict, cancel=None) -> None:
    _cancelled(cancel)
    if expected.get("size") is not None and path.stat().st_size != expected["size"]:
        raise ValueError("材料大小不符或仍是 LFS 指针：" + path.name)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            _cancelled(cancel)
            digest.update(block)
    if digest.hexdigest().lower() != expected["sha256"].lower():
        raise ValueError("SHA256 校验失败：" + path.name)


class _HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise ValueError("下载重定向必须使用 HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_file(component: dict, destination: Path, cancel=None, progress=None) -> str:
    """Retry transient I/O only; integrity failures never select a different version."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _HTTPSRedirect())
    errors = []
    for source in sources(component):
        url = source["url"]
        if urlsplit(url).scheme != "https":
            raise ValueError("下载地址必须使用 HTTPS")
        for attempt in range(2):
            _cancelled(cancel)
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "TF-LabControl/0.3"})
                deadline = time.monotonic() + 600
                with opener.open(request, timeout=15) as response, destination.open("wb") as stream:
                    received = 0
                    while True:
                        _cancelled(cancel)
                        if time.monotonic() > deadline:
                            raise TimeoutError("单个材料下载超过十分钟，可重试")
                        block = response.read(256 * 1024)
                        if not block:
                            break
                        received += len(block)
                        if received > component["size"]:
                            raise ValueError("下载内容超过登记大小；链接可能已更新")
                        stream.write(block)
                        if progress:
                            progress(
                                {
                                    "id": component["id"],
                                    "state": "downloading",
                                    "received": received,
                                    "total": component["size"],
                                    "source": url,
                                }
                            )
                check_file(destination, component, cancel)
                return url
            except (urllib.error.URLError, OSError) as exc:
                if isinstance(exc, InterruptedError):
                    raise
                errors.append(str(exc))
                if isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code < 500:
                    break
    raise OSError("固定版本下载失败；未回退其他版本：" + "; ".join(errors))


def verify_payload(component: dict, path: Path, staging: Path, cancel=None) -> dict:
    """Verify the archive before extracting only explicitly catalogued members."""
    check_file(path, component, cancel)
    members = component.get("archive_members", [])
    signature_path = path
    if members:
        with zipfile.ZipFile(path) as archive:
            names = set()
            for info in archive.infolist():
                safe_relative(info.filename.rstrip("/"))
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError("材料归档包含符号链接")
                normalized = info.filename.replace("\\", "/").casefold()
                if normalized in names:
                    raise ValueError("材料归档包含重复文件名")
                names.add(normalized)
            for item in members:
                _cancelled(cancel)
                info = archive.getinfo(item["member"])
                if info.file_size != item["size"]:
                    raise ValueError("归档成员大小不符：" + item["member"])
                target = staging / safe_relative(item["path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as incoming, target.open("wb") as outgoing:
                    while block := incoming.read(1024 * 1024):
                        _cancelled(cancel)
                        outgoing.write(block)
                check_file(target, item, cancel)
                if item["member"] == component["signature"].get("member"):
                    signature_path = target
    signature = component["signature"]
    if signature.get("member") and signature_path == path:
        raise ValueError("签名成员缺少受信解压映射")
    policy = signature.get("policy")
    if policy == "authenticode":
        from lab_control.deployment.windows import signature_status

        signed = signature_status(signature_path)
        if (
            signed.get("status") != "Valid"
            or not signature.get("publisher")
            or signature["publisher"].casefold() not in (signed.get("subject") or "").casefold()
        ):
            raise ValueError("厂商签名校验失败：" + str(signed))
        return {"signature": signed}
    if policy == "official-sha256" and signature.get("evidence"):
        return {"signature": {"status": "NotSigned", "verification": "pinned official SHA256"}}
    raise ValueError("尚未登记材料验证策略")


def prepare_component(
    component: dict,
    *,
    cache: Path | None = None,
    caches=None,
    cancel=None,
    progress=None,
    download: bool = True,
) -> dict:
    """Prepare in a per-user cache; an OS lease prevents overlapping cache mutations."""
    from lab_control.application.leases import FileLease

    cache = (cache or user_cache()).resolve()
    result = {"id": component["id"], "state": "blocked", "installed": False, "hardware_accepted": False}
    lease = None
    try:
        _cancelled(cancel)
        if not component.get("filename") or not component.get("sha256") or not component.get("size"):
            raise ValueError("固定版本、文件大小或哈希待核实；请查看官方来源")
        relative = safe_relative(
            component.get("cache_subdirectory", "") + "/" + component["filename"]
            if component.get("cache_subdirectory")
            else component["filename"]
        )
        target = cache / relative
        cache.mkdir(parents=True, exist_ok=True)
        lease = FileLease(cache / ".locks" / (component["id"] + ".lock"))
        candidates = tuple(caches) if caches is not None else cache_directories()
        status = payload_status(component, (cache, *candidates), cancel)
        if status["state"] == "cancelled":
            raise InterruptedError("准备已取消")
        with tempfile.TemporaryDirectory(prefix=".prepare-", dir=cache) as temporary:
            stage = Path(temporary)
            incoming = stage / component["filename"]
            if status["state"] == "verified":
                shutil.copyfile(status["path"], incoming)
                result["source"] = "verified cache"
            elif download:
                result["source"] = download_file(component, incoming, cancel, progress)
            else:
                raise FileNotFoundError("缺少完整且哈希匹配的离线材料：" + component["id"])
            result.update(verify_payload(component, incoming, stage / "extracted", cancel))
            _cancelled(cancel)
            # Validate all destinations before publishing. Reuse identical files,
            # including DLLs which may already be loaded by an active session.
            destinations = [
                (stage / "extracted" / safe_relative(item["path"]), cache / safe_relative(item["path"]), item)
                for item in component.get("archive_members", [])
            ]
            destinations.append((incoming, target, component))
            for _, destination, _ in destinations:
                if not destination.resolve().is_relative_to(cache):
                    raise ValueError("缓存路径经链接越界")
            for origin, destination, expected in destinations:
                try:
                    check_file(destination, expected)
                except (OSError, ValueError):
                    pass
                else:
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(origin, destination)
        result.update(
            state="prepared", path=str(target), sha256=component["sha256"], size=target.stat().st_size
        )
    except (InterruptedError, KeyboardInterrupt) as exc:
        result.update(state="cancelled", error=str(exc))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        result["error"] = str(exc)
    except Exception as exc:
        # FileLease reports ownership using the application's DeviceError.
        if getattr(exc, "code", "") != "resource_busy":
            raise
        result.update(state="busy", error=str(exc))
    finally:
        if lease is not None:
            lease.close()
    log = cache / "reports" / (component["id"] + "-" + str(time.time_ns()) + ".json")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if progress:
        progress(result)
    return result


def verify_component(component: dict, caches=None, cancel=None) -> dict:
    result = {"id": component["id"], "installed": False, "hardware_accepted": False}
    status = payload_status(component, tuple(caches) if caches is not None else cache_directories(), cancel)
    result.update(status)
    if status["state"] == "verified":
        try:
            with tempfile.TemporaryDirectory(prefix="lab-material-verify-") as temp:
                result.update(verify_payload(component, Path(status["path"]), Path(temp), cancel))
        except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
            result.update(
                state="cancelled" if isinstance(exc, InterruptedError) else "invalid", error=str(exc)
            )
    return result


def prepare_components(ids, *, cache=None, caches=None, cancel=None, progress=None, download=True) -> dict:
    results: list[dict] = []
    for component in component_closure(ids):
        if cancel and cancel.is_set():
            break
        if any(r["state"] != "prepared" and r["id"] in component.get("dependencies", []) for r in results):
            result = {"id": component["id"], "state": "blocked", "error": "前置组件未准备完成"}
            results.append(result)
            if progress:
                progress(result)
            continue
        results.append(
            prepare_component(
                component, cache=cache, caches=caches, cancel=cancel, progress=progress, download=download
            )
        )
    return {
        "schema_version": 1,
        "kind": "component_preparation",
        "items": results,
        "state": "cancelled" if cancel and cancel.is_set() else "complete",
        "passed": bool(results)
        and all(r["state"] == "prepared" for r in results)
        and not (cancel and cancel.is_set()),
        "installed": False,
        "hardware_accepted": False,
    }


def components_cli(args) -> dict:
    if args.component_action == "list":
        return {
            "components": [
                {
                    k: c.get(k)
                    for k in (
                        "id",
                        "name",
                        "purpose",
                        "version",
                        "size",
                        "download_sources",
                        "source_page",
                        "dependencies",
                    )
                }
                for c in load_manifest()["components"]
            ]
        }
    if not args.component:
        raise ValueError("至少使用一次 --component 指定组件")
    if args.component_action == "verify":
        items = [
            verify_component(c, (args.cache,) if args.cache else None)
            for c in component_closure(args.component)
        ]
        return {"items": items, "passed": all(i["state"] == "verified" for i in items)}
    cancel = threading.Event()
    return prepare_components(args.component, cache=args.cache, cancel=cancel, download=not args.offline)
