from dataclasses import replace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QVBoxLayout, QWidget

from lab_control.application.config import ConnectionConfig
from lab_control.application.controls import control_capabilities
from lab_control.application.leases import ResourceLeases
from lab_control.application.runtime import ApplicationRuntime
from lab_control.application.sessions import SessionManager
from lab_control.discovery.service import DiscoveryCandidate, Inventory
from lab_control.domain.instruments import DeviceCommand, DeviceError, DeviceIdentity, Parameter
from lab_control.instruments.registry import PROFILES
from lab_control.instruments.signal_sources import SignalGeneratorAdapter
from lab_control.storage.repository import Repository
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.pages.device_control.editors import ArrayEditor, ParameterEditor
from lab_control.ui.pages.device_control.form import OperationEditor


class ReadonlySma:
    """Fail on every unexpected query or setting write, including accidental output changes."""

    def __init__(self, serial="A", maximum=12.75e9):
        self.history = []
        self.fail = False
        self.maximum = maximum
        self.serial = serial

    def query(self, command):
        self.history.append(command)
        if self.fail and command == "SOUR:POW? MAX":
            raise DeviceError("Invalid range response", "parse_error")
        values = {
            "*IDN?": f"Rohde&Schwarz,SMA100B,{self.serial},4.60",
            "*OPT?": "SMAB-B112",
            "SOUR:POW? MIN": -145,
            "SOUR:POW? MAX": 40,
            "SOUR:POW:OFFS?": 0,
            "SOUR:POW:LIM?": 20,
            "SOUR:POW?": 21,
            "OUTP?": 0,
        }
        for prefix in ("SOUR:FREQ", "SOUR:FREQ:STAR", "SOUR:FREQ:STOP"):
            values[prefix + "? MIN"] = 8000
            values[prefix + "? MAX"] = self.maximum
        assert command in values, command
        return str(values[command]).encode()

    def write(self, command):
        pytest.fail(f"Unexpected instrument write: {command}")

    def close(self):
        pass


def test_worker_snapshot_atomic_refresh_isolation_and_strict_backend(tmp_path):
    repo = Repository(tmp_path)
    transports = {"one": ReadonlySma("A"), "two": ReadonlySma("B", 6e9)}
    sessions = SessionManager(
        ResourceLeases(),
        repo,
        lambda c: SignalGeneratorAdapter(transports[c.resource], PROFILES[c.adapter], c),
    )
    try:
        for resource in transports:
            config = ConnectionConfig(resource, "rs.sma100b", limits={"power": 30})
            sessions.connect(resource, config).result()
        snap = sessions.capability_snapshot("one")
        assert snap and not snap.stale and snap.options == ("SMAB-B112",)
        assert snap.channels[1].get("frequency").parameters["value"].maximum == 12.75e9
        assert snap.channels[1].get("power").parameters["value"].maximum == 30
        assert snap.channels[1].get("power").parameters["value"].range_source == "experiment"
        assert snap.details["rf_output_limit_dbm"] == 20
        assert (
            sessions.capability_snapshot("two").channels[1].get("frequency").parameters["value"].maximum
            == 6e9
        )
        with pytest.raises(DeviceError, match="above"):
            sessions.submit("one", "apply", DeviceCommand("frequency", {"value": 13e9})).result()
        with pytest.raises(DeviceError, match="above"):
            sessions.submit("one", "apply", DeviceCommand("power", {"value": 31})).result()
        transports["one"].fail = True
        transports["one"].maximum = 20e9
        with pytest.raises(DeviceError):
            sessions.submit("one", "refresh_capabilities").result()
        stale = sessions.capability_snapshot("one")
        assert stale.stale and stale.version > snap.version
        freq = next(c for c in stale.channels[1].operations if c.name == "frequency")
        assert not freq.available and freq.parameters["value"].maximum == 12.75e9
        assert freq.parameters["value"].range_source == "unavailable"
        assert not sessions.capability_snapshot("two").stale
        with pytest.raises(DeviceError):
            sessions.submit("one", "apply", DeviceCommand("power", {"value": 0})).result()
        sessions.disconnect("one")
        transports["one"].fail = False
        sessions.connect("one", ConnectionConfig("one", "rs.sma100b")).result()
        assert (
            sessions.capability_snapshot("one").channels[1].get("frequency").parameters["value"].maximum
            == 20e9
        )
    finally:
        sessions.close()
        repo.close()


def test_channel_and_experiment_limits_never_expand_model():
    config = ConnectionConfig("USB::example", "siglent.spd4306x", limits={"voltage": 1000})
    for channel, limits in PROFILES[config.adapter].channel_limits.items():
        cap = control_capabilities(config, channel).get("voltage")
        assert cap.parameters["value"].maximum <= limits["voltage"]
    config = ConnectionConfig("TCPIP::example", "rs.sma100b", limits={"power": -20})
    assert control_capabilities(config).get("power").parameters["value"].maximum == -20


@pytest.mark.parametrize(
    "text,expected", [("100", 40), ("-200", -145), ("-145", -145), ("40", 40), ("-2.1e1", -21)]
)
def test_frontend_bounds_and_scientific_notation(qtbot, text, expected):
    editor = ParameterEditor(Parameter(unit="dBm", minimum=-145, maximum=40, range_source="instrument"))
    qtbot.addWidget(editor)
    editor.input.setText(text)
    editor.finish_editing()
    assert editor.value() == expected
    assert "实机范围" in editor.range_label.text()
    if float(text) != expected:
        assert "已调整" in editor.feedback.text()


@pytest.mark.parametrize(
    "kind,text",
    [
        ("number", ""),
        ("number", "no"),
        ("number", "NaN"),
        ("number", "inf"),
        ("number", "1e999"),
        ("integer", "1.5"),
    ],
)
def test_invalid_input_is_not_replaced(qtbot, kind, text):
    editor = ParameterEditor(Parameter(kind, minimum=0, maximum=100))
    qtbot.addWidget(editor)
    editor.input.setText(text)
    editor.finish_editing()
    assert editor.input.text() == text
    assert editor.feedback.text()
    with pytest.raises(DeviceError):
        editor.value()


def test_final_unit_focus_enter_and_dynamic_edit(qtbot):
    parent = QWidget()
    qtbot.addWidget(parent)
    layout = QVBoxLayout(parent)
    editor = ParameterEditor(Parameter(unit="Hz", minimum=8000, maximum=12.75e9))
    other = ParameterEditor(Parameter(unit="dBm", minimum=-145, maximum=40))
    layout.addWidget(editor)
    layout.addWidget(other)
    parent.show()
    editor.input.setFocus()
    editor.input.setText("20")
    editor.unit.setFocus()
    qtbot.wait(5)
    assert editor.input.text() == "20"  # No premature 8000 Hz clamp before GHz is selected.
    editor.unit.setCurrentIndex(editor.unit.findText("GHz"))
    assert editor.input.text() == "12.75"
    assert "12.75 GHz" in editor.range_label.text()
    editor.input.setFocus()
    editor.input.setText("9")
    editor.set_spec(replace(editor.spec, maximum=6e9))
    assert editor.input.text() == "9"
    qtbot.keyClick(editor.input, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: editor.input.text() == "6")
    editor.input.setText("0")
    other.input.setFocus()
    qtbot.waitUntil(lambda: float(editor.input.text()) == 8e-6)


def test_unit_change_without_new_text_preserves_readback_target(qtbot):
    editor = ParameterEditor(Parameter(unit="Hz", minimum=8000, maximum=12.75e9))
    qtbot.addWidget(editor)
    assert "8 kHz ～ 12.75 GHz" in editor.range_label.text()
    editor.set_value(102e6)
    editor.unit.setCurrentIndex(editor.unit.findText("MHz"))
    assert editor.input.text() == "102" and editor.value() == 102e6
    editor.input.setText("20")
    editor.unit.setCurrentIndex(editor.unit.findText("GHz"))
    assert editor.input.text() == "12.75" and editor.value() == 12.75e9


def test_manual_table_clamps_but_import_is_atomic(qtbot, tmp_path):
    editor = ArrayEditor({"powers": Parameter("array", unit="dBm", minimum=-145, maximum=40)})
    qtbot.addWidget(editor)
    editor.model.replace_rows([[0]])
    assert editor.model.setData(editor.model.index(0, 0), "1e3")
    assert editor.values() == {"powers": [40]}
    assert not editor.model.setData(editor.model.index(0, 0), "nan")
    editor.set_parameters({"powers": replace(editor.parameters["powers"], maximum=30)})
    assert editor.values() == {"powers": [30]}
    path = tmp_path / "invalid.csv"
    path.write_text("power\n-20\n45\n", encoding="utf8")
    with pytest.raises(DeviceError, match="第 2 行"):
        editor.load_file(path)
    assert editor.values() == {"powers": [30]}


def test_draft_clamps_without_device_write_and_readback_is_unmodified(qtbot, tmp_path):
    config = ConnectionConfig("mock://draft", "rs.sma100b", "mock")
    with ApplicationRuntime((config,), root=tmp_path, hardware=False) as runtime:
        device = runtime.add(config)
        runtime.connect(device).result()
        parent = QWidget()
        qtbot.addWidget(parent)
        controller = WorkspaceController(runtime, parent)
        editor = OperationEditor(controller, device, controller.capabilities(device).get("power"), 1)
        qtbot.addWidget(editor)
        editor.editors["value"].input.setText("999")
        editor.editors["value"].finish_editing()
        draft = runtime.repository.load_workspace(editor.key)
        assert float(draft["fields"]["value"]["value"]) == 18
        from lab_control.domain.instruments import ParameterReading

        editor.show_reading(ParameterReading("power", 1, 21, "instrument"))
        assert "21" in editor.readback.text()
        assert editor.parameters() == {"value": 18}
        restored = OperationEditor(controller, device, editor.cap, 1)
        qtbot.addWidget(restored)
        assert restored.parameters() == {"value": 18}
        assert runtime.sessions.sessions[device].configuration_version == 0
        controller.close()


def test_imported_draft_survives_restart_and_tighter_range(qtbot, tmp_path):
    from lab_control.domain.instruments import Capability

    config = ConnectionConfig("mock://import", "rs.sma100b", "mock")
    with ApplicationRuntime((config,), root=tmp_path, hardware=False) as runtime:
        device = runtime.add(config)
        parent = QWidget()
        qtbot.addWidget(parent)
        controller = WorkspaceController(runtime, parent)
        cap = Capability("import_fixture", {"powers": Parameter("array", minimum=-145, maximum=40)})
        editor = OperationEditor(controller, device, cap, 1)
        qtbot.addWidget(editor)
        path = tmp_path / "experiment.csv"
        path.write_text("35\n", encoding="utf8")
        editor.array_editor.load_file(path)
        assert runtime.repository.load_workspace(editor.key)["array_path"] == str(path)
        tighter = replace(cap, parameters={"powers": replace(cap.parameters["powers"], maximum=30)})
        restored = OperationEditor(controller, device, tighter, 1)
        qtbot.addWidget(restored)
        assert restored.array_editor.model.rows == [[35]]
        with pytest.raises(DeviceError, match="above"):
            restored.array_editor.values()
        assert path.read_text(encoding="utf8") == "35\n"
        controller.close()


def test_verified_visa_alias_preferred_preserving_limits_and_manual_choice(tmp_path):
    repo = Repository(tmp_path)
    inventory = Inventory(repo)
    identity = DeviceIdentity("Rohde&Schwarz", "SMA100B", "123")
    tcp = ConnectionConfig("tcp://192.0.2.1:5025", "rs.sma100b", "tcp", limits={"power": -10})
    visa = ConnectionConfig("TCPIP0::192.0.2.1::inst0::INSTR", "rs.sma100b")
    try:
        record = inventory.merge(DiscoveryCandidate(tcp, "configured"), "scan1", identity, tcp.adapter)
        inventory.merge(DiscoveryCandidate(visa, "visa"), "scan2")
        assert record["config"]["transport"] == "tcp"  # Unverified alias cannot change primary.
        record = inventory.merge(DiscoveryCandidate(visa, "visa"), "scan3", identity, visa.adapter)
        assert record["config"]["transport"] == "visa"
        assert record["config"]["limits"] == {"power": -10}
        inventory.select_resource(record["id"], tcp.resource)
        inventory.merge(DiscoveryCandidate(visa, "visa"), "scan4", identity, visa.adapter)
        assert record["config"]["transport"] == "tcp"
    finally:
        repo.close()


def test_cli_rejects_out_of_range_without_writing(tmp_path, monkeypatch, capsys):
    from lab_control.cli.main import main

    transport = ReadonlySma()
    monkeypatch.setattr(
        "lab_control.application.runtime.create_adapter",
        lambda config, _visa: SignalGeneratorAdapter(transport, PROFILES[config.adapter], config),
    )
    path = tmp_path / "value.json"
    path.write_text('{"value": 13000000000}', encoding="utf8")
    assert (
        main(
            [
                "--mock",
                "--home",
                str(tmp_path),
                "--resource",
                "mock://sma",
                "--adapter",
                "rs.sma100b",
                "set",
                "frequency",
                "--parameters",
                str(path),
                "--write",
            ]
        )
        == 2
    )
    assert "above 12750000000" in capsys.readouterr().out
    assert all("?" in command for command in transport.history)
