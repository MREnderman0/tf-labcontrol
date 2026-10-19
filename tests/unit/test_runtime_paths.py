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
