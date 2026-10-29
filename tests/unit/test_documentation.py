from __future__ import annotations

import json
import runpy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

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


def test_history_redirect_templates_and_current_commands(tmp_path):
    for folder in ("docs/reviews", "docs/testing/templates"):
        (tmp_path / folder).mkdir(parents=True)
    (tmp_path / "README.md").write_text(
        "[Current](docs/current.md) [History](docs/reviews/old.md) [Template](docs/testing/templates/asset-status.md)"
    )
    (tmp_path / "docs/current.md").write_text("# Current\n```powershell\nuv run pytest\n```\n")
    (tmp_path / "docs/reviews/old.md").write_text("# Previous batch\n")
    (tmp_path / "docs/testing/templates/asset-status.md").write_text("# Asset\n")
    (tmp_path / "docs/redirect.md").write_text("<!-- docs: redirect -->\n# 文档已迁移\n", encoding="utf-8")
    _, errors = CHECKER["check"](tmp_path)
    assert any("bare uv command" in e for e in errors)
    assert any("missing historical marker" in e for e in errors)
    assert any("missing template marker" in e for e in errors)
    assert any("asset-status.md: missing template section" in e for e in errors)
    assert any("redirect must link to one" in e for e in errors)


def test_redirect_chains_and_preserved_anchors(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text("[Current](docs/current.md)")
    target = tmp_path / "docs/current.md"
    target.write_text("# Current\n## Original heading\n")
    old = tmp_path / "docs/old.md"
    old.write_text(
        '<!-- docs: redirect -->\n# 文档已迁移\n<a id="original-heading"></a>\n[New](current.md)',
        encoding="utf-8",
    )
    _, errors = CHECKER["check"](tmp_path)
    assert not any("redirect" in e for e in errors)
    target.write_text("# Current\n")
    _, errors = CHECKER["check"](tmp_path)
    assert any("preserved redirect anchors" in e for e in errors)
    target.write_text("<!-- docs: redirect -->\n# 文档已迁移\n[Old](old.md)", encoding="utf-8")
    _, errors = CHECKER["check"](tmp_path)
    assert any("canonical document" in e for e in errors)


def test_git_documents_include_design_deployment_but_ignore_cache(tmp_path, monkeypatch):
    paths = ["README.md", "design/reviews/sample.md", "deployment/packages/README.md"]
    contents = [
        "[Review](design/reviews/sample.md) [Materials](deployment/packages/README.md)",
        "<!-- docs: historical -->\n# Batch\nuv run old-command\n",
        "# Materials\n[Broken](absent.md)",
    ]
    for relative, content in zip(paths, contents, strict=True):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (tmp_path / "deployment/packages/local-cache.md").write_text("[Ignored](also-absent.md)")

    def git(command, **kwargs):
        if "rev-parse" in command:
            return SimpleNamespace(stdout=str(tmp_path).encode("utf-8"))
        assert "--exclude-standard" in command
        return SimpleNamespace(stdout="\0".join(paths).encode("utf-8"))

    monkeypatch.setattr("subprocess.run", git)
    count, errors = CHECKER["check"](tmp_path)
    assert count == 3
    assert any("deployment" in e and "missing local link" in e for e in errors)
    assert not any("local-cache" in e or "bare uv command" in e or "historical" in e for e in errors)


def test_fenced_code_does_not_create_navigation_or_heading_anchors():
    text = "# Actual\n````markdown\n```\n# Fake\n[Fake](missing.md)\n```\n````\n~~~\n# Also fake\n~~~"
    assert CHECKER["anchors"](text) == {"actual"}


def test_invalid_utf8_is_reported_without_crashing(tmp_path):
    (tmp_path / "README.md").write_bytes(b"\xff")
    _, errors = CHECKER["check"](tmp_path)
    assert "README.md: invalid UTF-8 text" in errors


def test_document_commands_are_checked_statically_and_never_executed(tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    source = scripts / "fixture.py"
    source.write_text(
        "raise RuntimeError('This script must never execute, even for --help')\n"
        "import argparse\nparser = argparse.ArgumentParser()\n"
        "parser.add_argument('--output')\n"
        "parser.add_argument('--mode', choices=('online', 'offline'))\n"
        "parser.parse_args()\n"
    )

    def forbidden(*args, **kwargs):
        pytest.fail("Documented commands must not be executed")

    monkeypatch.setattr("subprocess.run", forbidden)
    check_commands = CHECKER["command_errors"]
    valid = "```powershell\n.venv-dev\\Scripts\\python.exe scripts/fixture.py `\n --output '<directory>' --mode=online\n```"
    assert check_commands(tmp_path, valid) == []
    invalid = "```powershell\npython scripts/absent.py\npython scripts/fixture.py --typo --mode invalid\n```"
    errors = check_commands(tmp_path, invalid)
    assert any("missing project command script" in error for _, error in errors)
    assert any("unknown command option --typo" in error for _, error in errors)
    assert any("invalid literal choice invalid" in error for _, error in errors)


def test_dynamic_arguments_and_non_shell_examples_are_not_guessed(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/dynamic.py").write_text(
        "import argparse\np = argparse.ArgumentParser()\n"
        "for flag in externally_defined_flags:\n    p.add_argument(flag)\n"
        "p.add_argument('--mode', choices=('a', 'b'))\n"
    )
    examples = (
        "```powershell\npython scripts/dynamic.py --generated $value --mode <mode>\n"
        "python scripts/<chosen-script>.py --flag\n```\n"
        "```python\npython scripts/nonexistent.py --fake\n```\n"
        "Inline historical example: `python scripts/absent.py`\n"
    )
    assert CHECKER["command_errors"](tmp_path, examples) == []
    (tmp_path / "scripts/shared.py").write_text(
        "from another_module import parser\np = parser()\np.add_argument('--local')\n"
    )
    assert (
        CHECKER["command_errors"](
            tmp_path, "```powershell\npython scripts/shared.py --imported-option value --local value\n```"
        )
        == []
    )


def test_formal_launcher_accepts_current_options_and_rejects_old_entry(tmp_path):
    (tmp_path / "run.bat").touch()
    app = tmp_path / "src/lab_control/app.py"
    app.parent.mkdir(parents=True)
    app.write_text("import argparse\np = argparse.ArgumentParser()\np.add_argument('--config')\n")
    commands = "```powershell\n./run.bat --config '<selected-config>'\npython -m lab_control --help\n```"
    assert CHECKER["command_errors"](tmp_path, commands) == []
    commands = "```powershell\nrun_real.bat\npython -m lab_control.app\nrun.bat --unknown\n```"
    errors = CHECKER["command_errors"](tmp_path, commands)
    assert any("obsolete launch entry" in error for _, error in errors)
    assert any("bypassed formal module entry" in error for _, error in errors)
    assert any("unknown command option --unknown" in error for _, error in errors)


@pytest.mark.parametrize(
    "entry",
    ["lab-control-cli", r".venv-dev\Scripts\lab-control-cli.exe", "python -m lab_control.cli.main"],
)
def test_registered_cli_entries_validate_options_without_execution(tmp_path, entry):
    source = tmp_path / "src/lab_control/cli/main.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "raise RuntimeError('Must not execute')\n"
        "import argparse\np = argparse.ArgumentParser()\np.add_argument('--config')\n"
    )
    assert CHECKER["command_errors"](tmp_path, f"```powershell\n{entry} --config '<config>'\n```") == []
    errors = CHECKER["command_errors"](tmp_path, f"```powershell\n{entry} --definitely-unknown\n```")
    assert any("unknown command option --definitely-unknown" in error for _, error in errors)


def test_historical_and_redirect_documents_do_not_validate_old_commands(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / "README.md").write_text("[History](docs/history.md)")
    old_commands = "```powershell\npython scripts/deleted.py --old-option\nrun_real.bat\n```\n"
    (docs / "history.md").write_text("<!-- docs: historical -->\n# History\n" + old_commands)
    (docs / "old.md").write_text("<!-- docs: redirect -->\n[History](history.md)\n" + old_commands)
    _, errors = CHECKER["check"](tmp_path)
    assert not any("command" in error or "launch entry" in error for error in errors)


@pytest.mark.parametrize("entry", ["python -m lab_control", "TF-LabControl-Agent.exe", "lab-control"])
def test_agent_connector_options_use_early_dispatch_parser(tmp_path, entry):
    source = tmp_path / "src/lab_control/agent/connector.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import argparse\np = argparse.ArgumentParser()\np.add_argument('--connection')\np.add_argument('--check', action='store_true')\n"
    )
    assert (
        CHECKER["command_errors"](
            tmp_path, f"```powershell\n{entry} --agent-connector --connection test.json --check\n```"
        )
        == []
    )
    errors = CHECKER["command_errors"](tmp_path, f"```powershell\n{entry} --agent-connector --typo\n```")
    assert any("--typo" in error for _, error in errors)


def test_all_categories_and_new_non_signal_module():
    generator = runpy.run_path(str(ROOT / "scripts/generate_device_docs.py"))
    existing = next(item for item in registry.DEVICE_DEFINITIONS.values() if item.profile is not None)
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


def test_capability_inventory_does_not_invent_operation_ui_entries():
    generator = runpy.run_path(str(ROOT / "scripts/generate_device_docs.py"))
    rendered = generator["render"]()
    matrix = json.loads(rendered[ROOT / "docs/generated/capability-matrix.json"])
    models = {record["adapter_id"]: record for record in matrix["models"]}
    assert models["gwinstek.gpd4303s"]["control_page"] == "实时监测 / 电源"
    assert models["coremorrow.xmt"]["control_page"] == "设备管理 / 电源"
    assert models["siglent.sdg2082x"]["control_page"] == "设备管理 / 信号源"
    # A capability or label does not prove the GUI exposes a button for it.
    assert all(not operation["ui_location"] for model in models.values() for operation in model["operations"])
    assert "ui_location_note" in matrix
