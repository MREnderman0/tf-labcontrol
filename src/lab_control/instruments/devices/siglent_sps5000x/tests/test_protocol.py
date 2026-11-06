import json
from pathlib import Path

import pytest

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError, ValidationError
from lab_control.instruments.registry import build_scpi_adapter, match
from lab_control.transport.base import MockTransport

FIXTURE = json.loads((Path(__file__).with_name("fixtures") / "control.json").read_text(encoding="utf-8"))
ADAPTER_ID = FIXTURE["adapter"]
SET_VOLTAGE = DeviceCommand("voltage", FIXTURE["parameters"], FIXTURE["channel"])


def control_exchanges():
    return [(query, reply.encode()) for query, reply in FIXTURE["identity_exchanges"] + FIXTURE["exchanges"]]


def build(exchanges, **config):
    transport = MockTransport(exchanges)
    transport.open()
    adapter = build_scpi_adapter(
        ConnectionConfig("tcp://fixture:5025", ADAPTER_ID, "tcp", **config), transport
    )
    return adapter, transport


def idn(model="SPS5041X", serial="SYNTHETIC", maker="Siglent Technologies"):
    return "*IDN?", f"{maker},{model},{serial},TEST\n".encode()


@pytest.mark.parametrize(
    ("model", "channels", "voltage", "current"),
    [
        ("SPS5041X", 1, 40, 30),
        ("SPS5042X", 1, 40, 60),
        ("SPS5043X", 1, 40, 90),
        ("SPS5044X", 2, 40, 30),
        ("SPS5045X", 3, 40, 30),
        ("SPS5051X", 1, 50, 10),
        ("SPS5081X", 1, 80, 15),
        ("SPS5082X", 1, 80, 30),
        ("SPS5083X", 1, 80, 45),
        ("SPS5084X", 2, 80, 15),
        ("SPS5085X", 3, 80, 15),
        ("SPS5161X", 1, 160, 7.5),
        ("SPS5162X", 1, 160, 15),
        ("SPS5163X", 1, 160, 22.5),
        ("SPS5164X", 2, 160, 7.5),
        ("SPS5165X", 3, 160, 7.5),
    ],
)
def test_exact_models_and_rated_channel_limits(model, channels, voltage, current):
    adapter, transport = build([idn(model)])
    identity = adapter.identify()
    assert match(identity) == ADAPTER_ID
    caps = adapter.capabilities_for_channel(channels)
    assert caps.channels == tuple(range(1, channels + 1))
    for name, maximum in (("voltage", voltage), ("current", current)):
        assert caps.get(name).parameters["value"].maximum == maximum
        adapter.validate_command(DeviceCommand(name, {"value": maximum}, channels))
        with pytest.raises(ValidationError, match="above"):
            adapter.apply(DeviceCommand(name, {"value": maximum + 0.001}, channels))
    with pytest.raises(ValidationError, match="Invalid channel"):
        adapter.read("voltage", channels + 1)
    assert transport.history == ["*IDN?"]


@pytest.mark.parametrize(
    "reply",
    [idn("SPS5000X"), idn("SPS5166X"), idn(maker="OTHER"), idn(serial=""), idn(serial="DIFFERENT")],
)
def test_unsafe_identity_never_allows_settings(reply):
    adapter, transport = build([reply], expected_serial="SYNTHETIC")
    with pytest.raises(DeviceError, match="Expected|Manufacturer|序列号"):
        adapter.identify()
    with pytest.raises(DeviceError, match="完整身份"):
        adapter.apply(DeviceCommand("voltage", {"value": 5}))
    assert transport.history == ["*IDN?"]


def test_validation_rejects_limit_resolution_and_output_confirmation_without_io():
    adapter, transport = build([idn("SPS5044X")], limits={"current": 1})
    adapter.identify()
    with pytest.raises(ValidationError, match="configured/channel limit"):
        adapter.apply(DeviceCommand("current", {"value": 1.001}, 2))
    with pytest.raises(ValidationError, match="分辨率"):
        adapter.apply(DeviceCommand("current", {"value": 0.1234}, 2))
    with pytest.raises(DeviceError, match="confirmation"):
        adapter.apply(DeviceCommand("all_outputs", {"value": True}))
    with pytest.raises(ValidationError, match="whole instrument"):
        adapter.apply(DeviceCommand("all_outputs", {"value": True}, 2, confirmed=True))
    assert transport.history == ["*IDN?"]


def test_setpoint_write_and_readback_target_same_channel():
    adapter, transport = build(control_exchanges())
    adapter.identify()
    result = adapter.apply(SET_VOLTAGE)
    assert result.outcome == "success" and result.readback == 5.125
    assert not transport.exchanges


@pytest.mark.parametrize("failed_at", [5, 6, 7])
def test_uncertain_settings_are_never_replayed(failed_at):
    exchanges = control_exchanges()[: failed_at + 1]
    command, _ = exchanges[-1]
    exchanges[-1] = command, DeviceError("timeout", "timeout")
    # For an unconfirmed write, simulate a transport write exception after it is attempted.
    adapter, transport = build(exchanges)
    adapter.identify()
    if failed_at == 5:
        write = transport.write

        def timeout_on_write(payload):
            write(payload)
            if payload == command:
                raise DeviceError("timeout", "timeout")

        transport.write = timeout_on_write
    result = adapter.apply(SET_VOLTAGE)
    assert result.outcome == ("uncertain" if failed_at == 5 else "partial")
    assert transport.history == [query for query, _ in exchanges]


@pytest.mark.parametrize(
    ("replies", "expected"),
    [
        ([("SYSTEM:DEVICE:ROLE? CH1", b"M_SERIES")], "独立模式"),
        ([("SYSTEM:DEVICE:ROLE? CH1", b"M_INDEPEN"), ("LIST? CH1", b"1")], "List"),
        (
            [
                ("SYSTEM:DEVICE:ROLE? CH1", b"M_INDEPEN"),
                ("LIST? CH1", b"0"),
                (":SOURCE:CTRL:VOLT:MODE? CH1", b"EXT_V"),
            ],
            "内部设定",
        ),
    ],
)
def test_special_modes_prevent_setpoint_write(replies, expected):
    adapter, transport = build([idn(), *replies])
    adapter.identify()
    with pytest.raises(ValidationError, match=expected):
        adapter.apply(DeviceCommand("voltage", {"value": 5}))
    assert not transport.exchanges


def output_preflight(channel):
    return [
        (f"SYSTEM:DEVICE:ROLE? CH{channel}", b"M_INDEPEN"),
        (f"LIST? CH{channel}", b"0"),
        (f"OUTPUT:LOCAL:ENABLE? CH{channel}", b"1"),
        (f":SOURCE:CTRL:VOLT:MODE? CH{channel}", b"LOCAL"),
        (f":SOURCE:VOLTAGE:SET? CH{channel}", b"5"),
        (f":SOURCE:CTRL:CURR:MODE? CH{channel}", b"LOCAL"),
        (f":SOURCE:CURRENT:SET? CH{channel}", b"1"),
    ]


def test_master_output_checks_all_channels_and_never_changes_enable_settings():
    adapter, transport = build(
        [
            idn("SPS5044X"),
            *output_preflight(1),
            *output_preflight(2),
            ("OUTPUT ON", b""),
            ("*OPC?", b"1"),
            ("OUTPUT?", b"1"),
            ("OUTPUT OFF", b""),
            ("*OPC?", b"1"),
            ("OUTPUT?", b"0"),
        ]
    )
    adapter.identify()
    for enabled in (True, False):
        result = adapter.apply(DeviceCommand("all_outputs", {"value": enabled}, confirmed=True))
        assert result.outcome == "success" and result.readback is enabled
    assert not transport.exchanges


def test_master_output_blocks_when_any_channel_exceeds_experiment_limits():
    channel_two = output_preflight(2)
    channel_two[-1] = ":SOURCE:CURRENT:SET? CH2", b"2"
    adapter, transport = build([idn("SPS5044X"), *output_preflight(1), *channel_two], limits={"current": 1})
    adapter.identify()
    with pytest.raises(ValidationError, match="configured/channel limit"):
        adapter.apply(DeviceCommand("all_outputs", {"value": True}, confirmed=True))
    assert not transport.exchanges


@pytest.mark.parametrize("bad_reply", [b"garbled", b"nan", b"inf", b"\xff"])
def test_invalid_scalar_read_fails_entire_batch(bad_reply):
    adapter, _ = build(
        [idn("SPS5044X"), ("MEASURE:VOLTAGE? CH2", b"4.9"), ("MEASURE:CURRENT? CH2", bad_reply)]
    )
    adapter.identify()
    with pytest.raises(DeviceError) as error:
        adapter.acquire(AcquisitionRequest("scalar", (2,)))
    assert error.value.code in {"parse_error", "overload"}


def test_real_session_publishes_identified_channels_and_acquires_via_shared_entry(tmp_path):
    from lab_control.application.runtime import ApplicationRuntime

    adapter, transport = build(
        [idn("SPS5165X"), ("MEASURE:VOLTAGE? CH3", b"4.9"), ("MEASURE:CURRENT? CH3", b"1.2")]
    )
    with ApplicationRuntime(root=tmp_path, hardware=False) as app:
        app.sessions.factory = lambda _: adapter
        key = app.add(ConnectionConfig("mock://test-sps5000x", ADAPTER_ID, "mock"))
        app.connect(key).result(5)
        snapshot = app.sessions.capability_snapshot(key)
        assert tuple(snapshot.channels) == (1, 2, 3)
        assert snapshot.channels[3].get("voltage").parameters["value"].maximum == 160
        result = app.sessions.submit(key, "acquire", AcquisitionRequest("scalar", (3,), points=1)).result(5)
        assert [(m.quantity, m.unit, m.channel, m.values[0]) for m in result.measurements] == [
            ("voltage", "V", 3, 4.9),
            ("current", "A", 3, 1.2),
        ]
    assert not transport.exchanges and transport.close_count == 1
