from __future__ import annotations

import runpy
from dataclasses import replace
from pathlib import Path

import pytest

from lab_control.instruments import registry

ROOT = Path(__file__).resolve().parents[2]
CHECKER = runpy.run_path(str(ROOT / "scripts/check_docs.py"))


def test_unicode_heading_anchors_and_duplicate_headings():
    assert CHECKER["anchors"]("# 单位 / 范围\n## Same\n## Same\n````\n# ignored\n````") >= {
        "单位--范围",
        "same",
        "same-1",
    }


def test_navigation_and_anchor_errors(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text("[Guide](docs/guide.md#missing)")
    (tmp_path / "docs/guide.md").write_text("# Guide")
    (tmp_path / "docs/orphan.md").write_text("# Orphan")
    _, errors = CHECKER["check"](tmp_path)
    assert any("missing heading anchor" in e for e in errors)
    assert any("unreachable" in e and "orphan" in e for e in errors)
    assert any("Missing template" in e for e in errors)


def test_all_categories_and_new_non_signal_module():
    generator = runpy.run_path(str(ROOT / "scripts/generate_device_docs.py"))
    existing = next(iter(registry.DEVICE_DEFINITIONS.values()))
    definition = replace(
        existing,
        profile=replace(existing.profile, adapter_id="test.new_power", category="power_supply"),
        documentation=replace(existing.documentation, card="power-supplies/test-added"),
    )
    rendered = generator["render"]({definition.adapter_id: definition})
    assert "test-added.md" in rendered[ROOT / "docs/devices/power-supplies/README.md"]
    for folder in generator["CATEGORY_FOLDERS"]:
        assert ROOT / "docs/devices" / folder / "README.md" in rendered
    assert "psw160.md" in rendered[ROOT / "docs/devices/power-supplies/README.md"]
    assert "goca-8.md" in rendered[ROOT / "docs/devices/measurement-analyzers/README.md"]


def test_duplicate_card_and_missing_maintenance_source(tmp_path):
    generator = runpy.run_path(str(ROOT / "scripts/generate_device_docs.py"))
    first, second, *_ = registry.DEVICE_DEFINITIONS.values()
    other = replace(second, documentation=replace(second.documentation, card=first.documentation.card))
    with pytest.raises(ValueError, match="Duplicate documentation card"):
        generator["render"]({first.adapter_id: first, other.adapter_id: other})
    with pytest.raises(ValueError, match="Missing model documentation source"):
        generator["render"]({first.adapter_id: replace(first, root=tmp_path)})
