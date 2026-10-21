import copy
import hashlib
import io
import json
import threading
import urllib.error
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from lab_control.deployment import materials
from lab_control.deployment.catalog import load_manifest, release_issues


def component(content=b"fixture"):
    return {
        "id": "fixture",
        "filename": "fixture.exe",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "url": "https://example.invalid/fixture.exe",
        "signature": {"policy": "official-sha256", "evidence": "test-only pinned fixture"},
        "archive_members": [],
        "dependencies": [],
    }


def cached(tmp_path, content=b"fixture"):
    source = tmp_path / "source"
    source.mkdir()
    (source / "fixture.exe").write_bytes(content)
    return source


def test_cache_reuse_and_download_are_separate_from_install(tmp_path, monkeypatch):
    source = cached(tmp_path)
    monkeypatch.setattr(materials, "download_file", lambda *a: pytest.fail("network"))
    target = tmp_path / "cache"
    result = materials.prepare_component(component(), cache=target, caches=(source,))
    assert result["state"] == "prepared"
    assert not result["installed"] and not result["hardware_accepted"]
    assert (target / "fixture.exe").read_bytes() == b"fixture"
    assert list((target / "reports").glob("*.json"))
    assert materials.verify_component(component(), (target,))["state"] == "verified"


@pytest.mark.parametrize("content", [b"bad", b"version https://git-lfs.github.com/spec/v1\n"])
def test_bad_cache_or_lfs_pointer_cannot_be_used_offline(tmp_path, content):
    source = cached(tmp_path, content)
    result = materials.prepare_component(
        component(), cache=tmp_path / "cache", caches=(source,), download=False
    )
    assert result["state"] == "blocked"
    assert not (tmp_path / "cache/fixture.exe").exists()


def test_signer_failure_does_not_publish_material(tmp_path, monkeypatch):
    source = cached(tmp_path)
    c = component()
    c["signature"] = {"policy": "authenticode", "publisher": "Expected"}
    monkeypatch.setattr(
        "lab_control.deployment.windows.signature_status",
        lambda path: {"status": "Valid", "subject": "Different"},
    )
    result = materials.prepare_component(c, cache=tmp_path / "cache", caches=(source,), download=False)
    assert result["state"] == "blocked"
    assert not (tmp_path / "cache/fixture.exe").exists()


def test_cancel_retains_usable_cache_and_logs_result(tmp_path):
    target = cached(tmp_path)
    cancel = threading.Event()
    cancel.set()
    result = materials.prepare_component(component(), cache=target, cancel=cancel)
    assert result["state"] == "cancelled"
    assert (target / "fixture.exe").read_bytes() == b"fixture"


def test_cancel_during_download_cleans_partial_and_retry_succeeds(tmp_path, monkeypatch):
    cancel = threading.Event()
    interrupted = {"once": True}

    class Response(io.BytesIO):
        def read(self, size=-1):
            block = super().read(size)
            if interrupted["once"]:
                interrupted["once"] = False
                cancel.set()
            return block

    class Opener:
        def open(self, request, timeout):
            return Response(b"fixture")

    monkeypatch.setattr(materials.urllib.request, "build_opener", lambda *a: Opener())
    result = materials.prepare_component(component(), cache=tmp_path, caches=(), cancel=cancel)
    assert result["state"] == "cancelled"
    assert not (tmp_path / "fixture.exe").exists()
    assert not list(tmp_path.glob(".prepare-*"))
    cancel.clear()
    assert (
        materials.prepare_component(component(), cache=tmp_path, caches=(), cancel=cancel)["state"]
        == "prepared"
    )


def test_explicit_legacy_cache_root_finds_installers_subdirectory(tmp_path):
    (tmp_path / "installers").mkdir()
    (tmp_path / "installers/fixture.exe").write_bytes(b"fixture")
    assert materials.verify_component(component(), (tmp_path,))["state"] == "verified"


def archive_component(tmp_path, extra=None):
    source = tmp_path / "source"
    source.mkdir()
    path = source / "fixture.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("library.dll", b"library")
        if extra:
            archive.writestr(extra, b"escape")
    c = component(path.read_bytes())
    c.update(
        filename="fixture.zip",
        archive_members=[
            {
                "member": "library.dll",
                "path": "tools/runtime/library.dll",
                "size": 7,
                "sha256": hashlib.sha256(b"library").hexdigest(),
            }
        ],
    )
    return c, source


def test_safe_archive_restores_pinned_members(tmp_path):
    c, source = archive_component(tmp_path)
    result = materials.prepare_component(c, cache=tmp_path / "cache", caches=(source,), download=False)
    assert result["state"] == "prepared"
    assert (tmp_path / "cache/tools/runtime/library.dll").read_bytes() == b"library"


@pytest.mark.parametrize("extra", ["../escape.dll", "/absolute.dll", "C:/escape.dll", "file:stream", "NUL"])
def test_archive_rejects_unsafe_unselected_members(tmp_path, extra):
    c, source = archive_component(tmp_path, extra)
    result = materials.prepare_component(c, cache=tmp_path / "cache", caches=(source,), download=False)
    assert result["state"] == "blocked"
    assert not (tmp_path / "cache/tools/runtime/library.dll").exists()


def test_member_hash_failure_preserves_old_runtime(tmp_path):
    c, source = archive_component(tmp_path)
    c["archive_members"][0]["sha256"] = "0" * 64
    target = tmp_path / "cache/tools/runtime/library.dll"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    result = materials.prepare_component(c, cache=tmp_path / "cache", caches=(source,), download=False)
    assert result["state"] == "blocked" and target.read_bytes() == b"old"


def test_concurrent_prepare_uses_os_lock_and_retries(tmp_path, monkeypatch):
    entered, leave = threading.Event(), threading.Event()

    def download(c, path, *args):
        entered.set()
        assert leave.wait(10)
        path.write_bytes(b"fixture")
        return c["url"]

    monkeypatch.setattr(materials, "download_file", download)
    target = tmp_path / "cache"
    with ThreadPoolExecutor() as pool:
        future = pool.submit(materials.prepare_component, component(), cache=target, caches=())
        assert entered.wait(5)
        try:
            other = materials.prepare_component(component(), cache=target, caches=())
            assert other["state"] == "busy"
        finally:
            leave.set()
        assert future.result(10)["state"] == "prepared"
    assert (
        materials.prepare_component(component(), cache=target, caches=(), download=False)["state"]
        == "prepared"
    )


def test_download_has_no_proxy_and_only_selected_source(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setenv("HTTPS_PROXY", "http://unused.invalid")

    class Opener:
        def open(self, request, timeout):
            seen.append(request.full_url)
            return io.BytesIO(b"fixture")

    def build(*handlers):
        assert handlers[0].proxies == {}
        return Opener()

    monkeypatch.setattr(materials.urllib.request, "build_opener", build)
    c = component()
    path = tmp_path / "download"
    assert materials.download_file(c, path) == c["url"]
    assert seen == [c["url"]]


def test_bad_download_hash_does_not_fallback(tmp_path, monkeypatch):
    seen = []

    class Opener:
        def open(self, request, timeout):
            seen.append(request.full_url)
            return io.BytesIO(b"changed")

    monkeypatch.setattr(materials.urllib.request, "build_opener", lambda *a: Opener())
    c = component()
    c["download_sources"] = [{"url": c["url"]}, {"url": "https://example.invalid/backup"}]
    with pytest.raises(ValueError, match="SHA256"):
        materials.download_file(c, tmp_path / "download")
    assert seen == [c["url"]]


def test_missing_url_is_reported_without_unbounded_retries(tmp_path, monkeypatch):
    seen = []

    class Opener:
        def open(self, request, timeout):
            seen.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, 404, "gone", {}, None)

    monkeypatch.setattr(materials.urllib.request, "build_opener", lambda *a: Opener())
    result = materials.prepare_component(component(), cache=tmp_path, caches=())
    assert result["state"] == "blocked" and "固定版本" in result["error"]
    assert len(seen) == 1


def test_online_release_preserves_other_gates(tmp_path):
    c = load_manifest()["components"][0]
    problems = release_issues(c, (tmp_path,), distribution="online")
    assert "offline payload missing" not in problems
    assert "redistribution not verified" in problems
    assert "validation not verified" in problems


def test_dependency_closure_order_cycle_and_unknown():
    first, second = component(), component()
    second.update(id="wrapper", dependencies=["fixture"])
    manifest = {"components": [first, second]}
    assert [c["id"] for c in materials.component_closure(["wrapper"], manifest)] == ["fixture", "wrapper"]
    first["dependencies"] = ["wrapper"]
    with pytest.raises(ValueError):
        materials.component_closure(["wrapper"], manifest)
    with pytest.raises(ValueError):
        materials.component_closure(["unknown"], manifest)


def test_legacy_url_and_manifest_validity():
    c = copy.deepcopy(load_manifest()["components"][0])
    c.pop("download_sources")
    assert materials.sources(c)[0]["url"] == c["url"]
    assert json.dumps(load_manifest())
