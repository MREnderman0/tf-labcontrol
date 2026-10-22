"""Dependency contracts for pure domain types and legacy import compatibility."""

import ast
from pathlib import Path

from lab_control.application import acquisition_pipeline, config
from lab_control.domain import connections, frames

ROOT = Path(__file__).resolve().parents[2] / "src/lab_control"


def imported_modules(path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
        elif isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)


def test_domain_is_independent_of_runtime_and_ui():
    forbidden = (
        "lab_control.application",
        "lab_control.storage",
        "lab_control.ui",
        "lab_control.instruments",
        "lab_control.transport",
        "PySide6",
    )
    for path in (ROOT / "domain").glob("*.py"):
        for module in imported_modules(path):
            assert not module.startswith(forbidden), (path, module)


def test_legacy_imports_preserve_type_and_function_identity():
    assert config.ConnectionConfig is connections.ConnectionConfig
    assert config.canonical_resource is connections.canonical_resource
    for name in (
        "FrozenDict",
        "immutable_batch",
        "batch_bytes",
        "validate_recorded_batch",
        "validate_displayed_batch",
    ):
        assert getattr(acquisition_pipeline, name) is getattr(frames, name)


def test_storage_frame_contracts_do_not_depend_on_application():
    for path in (ROOT / "storage").glob("*.py"):
        assert "lab_control.application.acquisition_pipeline" not in set(imported_modules(path)), path


def test_discovery_and_protocol_configuration_uses_domain_contract():
    paths = [
        *(ROOT / "discovery").glob("*.py"),
        ROOT / "instruments/base.py",
        ROOT / "instruments/registry.py",
    ]
    for path in paths:
        assert "lab_control.application.config" not in set(imported_modules(path)), path


def test_workspace_helpers_do_not_read_instrument_handles():
    forbidden = ("lab_control.transport", "pyvisa", "serial")
    for path in (ROOT / "ui/workspace").glob("*.py"):
        assert not any(module.startswith(forbidden) for module in imported_modules(path)), path
