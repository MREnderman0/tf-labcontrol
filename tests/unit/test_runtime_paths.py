from pathlib import Path

import pytest

from lab_control.domain.instruments import DeviceError
from lab_control.storage import runtime


def test_runtime_home_uses_the_inventory_final_parent(monkeypatch, tmp_path):
    logical = tmp_path / "logical" / "TF-LabControl"
    physical = tmp_path / "physical" / "TF-LabControl"
    logical.mkdir(parents=True)
    physical.mkdir(parents=True)
    marker = logical / "inventory.sqlite3"
    marker.touch()
    monkeypatch.setenv("LAB_CONTROL_HOME", str(logical))
    monkeypatch.setattr(runtime, "physical_path", lambda path: physical / path.name)

    assert runtime.runtime_home() == physical


def test_runtime_home_keeps_explicit_path_when_inventory_is_unavailable(monkeypatch, tmp_path):
    logical = tmp_path / "TF-LabControl"
    logical.mkdir()
    monkeypatch.setenv("LAB_CONTROL_HOME", str(logical))
    monkeypatch.setattr(runtime, "physical_path", lambda path: (_ for _ in ()).throw(OSError("unavailable")))

    assert runtime.runtime_home() == logical


def test_default_packaged_copy_is_rejected_before_repository_creation(monkeypatch, tmp_path):
    logical = tmp_path / "TF-LabControl"
    logical.mkdir()
    (logical / "inventory.sqlite3").touch()
    private = tmp_path / "Packages" / "Example.App" / "LocalCache" / "Local" / logical.name
    monkeypatch.delenv("LAB_CONTROL_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(runtime, "physical_path", lambda path: private / path.name)

    with pytest.raises(DeviceError) as error:
        runtime.runtime_home()
    assert error.value.code == "path_redirected"
    assert "LAB_CONTROL_HOME" in str(error.value)
    assert not private.exists()


def test_explicit_runtime_still_rejects_unexpected_packaged_redirection(monkeypatch, tmp_path):
    logical = tmp_path / "TF-LabControl"
    logical.mkdir()
    (logical / "inventory.sqlite3").touch()
    private = tmp_path / "Packages" / "Example.App" / "LocalCache" / "Local" / logical.name
    monkeypatch.setenv("LAB_CONTROL_HOME", str(logical))
    monkeypatch.setattr(runtime, "physical_path", lambda path: private / path.name)
    with pytest.raises(DeviceError) as error:
        runtime.runtime_home()
    assert error.value.code == "path_redirected"


def test_packaged_redirect_detection_preserves_normal_runtime_paths():
    logical = Path("C:/Users/Example/AppData/Local/TF-LabControl")
    private = Path("C:/Users/Example/AppData/Local/Packages/App/LocalCache/Local/TF-LabControl")
    assert runtime._packaged_redirect(logical, private)
    assert not runtime._packaged_redirect(logical, logical)
    assert not runtime._packaged_redirect(logical, Path("D:/LabData/TF-LabControl"))


@pytest.mark.skipif(runtime.os.name != "nt", reason="Windows handle path normalization")
def test_physical_path_preserves_unc_prefix(monkeypatch, tmp_path):
    # The native API returns \\?\UNC\server\share; dropping only \\?\
    # would turn that absolute UNC location into a relative path.
    import ctypes

    real_dll = ctypes.WinDLL

    class Function:
        def __init__(self, function):
            self.function = function

        def __call__(self, *args):
            return self.function(*args)

    class Kernel:
        def __init__(self):
            real = real_dll("kernel32", use_last_error=True)
            self.CreateFileW = real.CreateFileW
            self.CloseHandle = real.CloseHandle

            def final_path(handle, buffer, size, flags):
                buffer.value = r"\\?\UNC\localhost\C$\runtime\inventory.sqlite3"
                return len(buffer.value)

            self.GetFinalPathNameByHandleW = Function(final_path)

    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: Kernel())
    assert runtime.physical_path(tmp_path) == Path(r"\\localhost\C$\runtime\inventory.sqlite3")
