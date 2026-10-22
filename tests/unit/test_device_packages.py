import ast
import hashlib
import json
import runpy
from pathlib import Path

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import DeviceIdentity, Unsupported, jsonable
from lab_control.instruments import registry
from lab_control.instruments.devices.catalog import DEVICE_MODULES, load_definitions
from lab_control.transport.base import MockTransport

ROOT = Path(__file__).parents[2]


@pytest.mark.parametrize("definition", registry.DEVICE_DEFINITIONS.values(), ids=registry.DEVICE_DEFINITIONS)
def test_declared_capabilities_units_ranges_and_wire_templates(definition):
    profile = definition.profile
    fixtures = definition.root / "tests/fixtures"
    # Keep the immutable migration evidence. Later model changes declare a new contract.
    assert (fixtures / "capabilities-before.json").is_file()
    contract = fixtures / "capabilities-current.json"
    if not contract.exists():
        contract = fixtures / "capabilities-before.json"
    before = json.loads(contract.read_text(encoding="utf-8"))
    # Older capability snapshots omit the newly optional resolution field.
    for operation in before["capabilities"]["operations"]:
        for parameter in operation["parameters"].values():
            parameter.setdefault("resolution", None)
    assert (
        jsonable(registry.describe(ConnectionConfig("metadata://test", definition.adapter_id)))
        == before["capabilities"]
    )
    if profile is None:
        assert before["profile"] is None and definition.adapter_type is None
        assert definition.factory is not None and definition.capability_schema is not None
        return
    assert jsonable({key: getattr(profile, key) for key in before["profile"]}) == before["profile"]
    actual = {
        name: {"writes": spec.writes, "query": spec.query, "select": spec.select, "compare": spec.compare}
        for name, spec in profile.commands.items()
    }
    assert jsonable(actual) == before["commands"]
    transport = MockTransport([])
    adapter = registry.build_scpi_adapter(ConnectionConfig("mock://test", profile.adapter_id), transport)
    assert type(adapter) is definition.adapter_type
    assert not transport.history


@pytest.mark.parametrize("definition", registry.DEVICE_DEFINITIONS.values(), ids=registry.DEVICE_DEFINITIONS)
def test_model_package_has_no_legacy_or_cross_model_imports(definition):
    own = "lab_control.instruments.devices." + definition.root.name
    shared = {"lab_control.instruments.devices.definition"}
    for path in definition.root.rglob("*.py"):
        if "tests" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            modules = (
                [node.module]
                if isinstance(node, ast.ImportFrom)
                else ([alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
            )
            for module in filter(None, modules):
                assert module not in {
                    "lab_control.instruments.signal_sources",
                    "lab_control.instruments.power_supplies",
                    "lab_control.instruments.oscilloscopes",
                    "lab_control.instruments.analyzers",
                    "lab_control.instruments.acquisition",
                    "lab_control.instruments.pico",
                    "lab_control.instruments.optical",
                }, path
                if "hardware" not in path.parts:
                    assert module != "lab_control.application.config", path
                if module.startswith("lab_control.instruments.devices."):
                    assert module in shared or module == own or module.startswith(own + "."), (path, module)


def test_registry_rejects_duplicate_model_ids():
    with pytest.raises(ValueError, match="Duplicate adapter ID"):
        load_definitions((DEVICE_MODULES[0], DEVICE_MODULES[0]))
    profile = next(iter(registry.PROFILES.values()))
    with pytest.raises(ValueError, match="Duplicate adapter ID"):
        registry.collect_profiles((profile, profile))


@pytest.mark.parametrize("removed", tuple(registry.DEVICE_DEFINITIONS))
def test_removed_model_is_unavailable_before_opening_any_resource(monkeypatch, removed):
    definition = registry.DEVICE_DEFINITIONS[removed]
    monkeypatch.delitem(registry.DEVICE_DEFINITIONS, removed)
    monkeypatch.delitem(registry.PROFILES, removed, raising=False)
    monkeypatch.delitem(registry.SDK_ADAPTERS, removed, raising=False)
    monkeypatch.setattr(
        registry, "transport_for", lambda *args: pytest.fail("Removed model opened transport")
    )
    metadata = definition.profile or definition.model
    identity = DeviceIdentity(metadata.manufacturer[0], metadata.models[0], "TEST")
    assert registry.match(identity) == ""
    config = ConnectionConfig("USB::TEST", removed, expected_serial="TEST")
    with pytest.raises(Unsupported):
        registry.describe(config)
    with pytest.raises(Unsupported, match="不可用或已移除"):
        registry.create_adapter(config, None)
    for other in registry.DEVICE_DEFINITIONS.values():
        assert registry.describe(ConnectionConfig("metadata://test", other.adapter_id)).operations
        if other.adapter_type is None:
            continue
        assert isinstance(
            registry.build_scpi_adapter(ConnectionConfig("mock://test", other.adapter_id), MockTransport([])),
            other.adapter_type,
        )


def test_registry_does_not_load_removed_package(monkeypatch):
    from lab_control.instruments.devices import catalog

    original = catalog.import_module
    removed = DEVICE_MODULES[0]

    def checked_import(module):
        assert module != removed
        return original(module)

    monkeypatch.setattr(catalog, "import_module", checked_import)
    definitions = catalog.load_definitions(tuple(m for m in DEVICE_MODULES if m != removed))
    assert len(definitions) == len(DEVICE_MODULES) - 1


def generator(tmp_path):
    module = runpy.run_path(str(ROOT / "scripts/generate_device_docs.py"))
    # Functions retain their defining namespace, which is distinct from runpy's returned mapping.
    namespace = module["obsolete_cards"].__globals__
    namespace["ROOT"] = tmp_path
    manifest = tmp_path / "docs/generated/device-module-docs.json"
    manifest.parent.mkdir(parents=True)
    return module, manifest


def test_removed_model_docs_cleanup_only_owns_unmodified_cards(tmp_path):
    module, manifest = generator(tmp_path)
    path = tmp_path / "docs/devices/signal-sources/test.md"
    path.parent.mkdir(parents=True)
    contents = module["MODEL_CARD_MARKER"] + "\n# Model\n"
    path.write_text(contents, encoding="utf-8")
    manifest.write_text(
        json.dumps({"docs/devices/signal-sources/test.md": hashlib.sha256(contents.encode()).hexdigest()})
    )
    assert module["obsolete_cards"]({manifest: "{}"}) == [path]
    path.write_text(contents + "Hand-written notes", encoding="utf-8")
    with pytest.raises(ValueError, match="manually modified"):
        module["obsolete_cards"]({manifest: "{}"})
    assert path.exists()


def test_generated_doc_manifest_cannot_escape_owned_directory(tmp_path):
    module, manifest = generator(tmp_path)
    manifest.write_text(json.dumps({"../user.md": "bad"}))
    with pytest.raises(ValueError, match="Unsafe"):
        module["obsolete_cards"]({manifest: "{}"})


@pytest.mark.parametrize("removed", tuple(registry.DEVICE_DEFINITIONS))
def test_removing_model_updates_every_generated_navigation(removed):
    module = runpy.run_path(str(ROOT / "scripts/generate_device_docs.py"))
    definition = registry.DEVICE_DEFINITIONS[removed]
    files = module["render"]({k: v for k, v in registry.DEVICE_DEFINITIONS.items() if k != removed})
    assert ROOT / f"docs/devices/{definition.documentation.card}.md" not in files
    for relative in (
        "docs/device-research-index.md",
        "docs/devices/signal-sources/README.md",
        "docs/devices/power-supplies/README.md",
        "docs/generated/capability-matrix.md",
        "docs/generated/capability-matrix.json",
    ):
        assert removed not in files[ROOT / relative]
        assert definition.documentation.card not in files[ROOT / relative]


@pytest.mark.parametrize(
    "command", ["OUTP1 ON", "OUTP1 OFF", "*RST", "*TRG", "*CLS", "SYST:ERR?", "SOUR1:FREQ 1000"]
)
def test_readonly_hardware_guard_blocks_every_setting_and_error_queue(tmp_path, command):
    from lab_control.domain.instruments import DeviceError
    from lab_control.instruments.common.readonly_hardware import QueryOnlyTransport

    transport = MockTransport([])
    guarded = QueryOnlyTransport(transport, {"*IDN?"}, tmp_path / "communication.jsonl")
    with pytest.raises(DeviceError, match="forbids"):
        guarded.write(command)
    assert not transport.history


def test_readonly_hardware_guard_logs_both_directions_without_replay(tmp_path):
    from lab_control.domain.instruments import DeviceError
    from lab_control.instruments.common.readonly_hardware import QueryOnlyTransport

    transport = MockTransport([("*IDN?", DeviceError("timeout", "timeout"))])
    transport.open()
    path = tmp_path / "communication.jsonl"
    guarded = QueryOnlyTransport(transport, {"*IDN?"}, path)
    with pytest.raises(DeviceError):
        guarded.query("*IDN?")
    assert transport.history == ["*IDN?"]
    assert [json.loads(line)["direction"] for line in path.read_text().splitlines()] == ["tx", "error"]


def test_readonly_asset_path_preserves_composite_serial_for_identity_checks():
    from lab_control.instruments.common.readonly_hardware import asset_folder_name

    assert asset_folder_name("TEST", "PART/TEST123") == "TEST-PART_TEST123"
    assert asset_folder_name("TEST", "PART/TEST123", "TEST-TEST123") == "TEST-TEST123"
    for invalid in ("..", "../escape", "C:/elsewhere", r"folder\child"):
        with pytest.raises(ValueError, match="single safe directory"):
            asset_folder_name("TEST", "PART/TEST123", invalid)
