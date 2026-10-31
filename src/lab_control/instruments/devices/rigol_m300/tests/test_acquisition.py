from dataclasses import replace

import pytest

from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError

from ..protocol import measurement_kind, scan_channels
from .test_protocol import adapter


def snapshot(
    channels=(428,),
    *,
    unit="C",
    config="TEMP THER,10000,+1.000000E+00,+3.000000E-07",
    trigger="IMM",
    count="1",
    format_on=False,
):
    replies = [
        ("ROUT:SCAN?", "(@" + ",".join(map(str, channels)) + ")"),
        ("ROUT:SCAN:SIZE?", str(len(channels))),
    ]
    for channel in channels:
        replies.append((f"CONF? (@{channel})", config))
        if config.startswith("TEMP"):
            replies.extend([(f"FUNC? (@{channel})", "TEMP"), (f"UNIT:TEMP? (@{channel})", unit)])
    replies.extend(
        [("TRIG:SOUR?", trigger), ("TRIG:COUN?", count), ("INST:DMM?", "1"), ("ROUT:MON:STAT?", "0")]
    )
    replies.extend(
        (f"FORM:READ:{key}?", "1" if format_on else "0") for key in ("UNIT", "TIME", "CHAN", "ALAR")
    )
    return [(q, a.encode()) for q, a in replies]


def input_adapter(exchanges):
    a, transport = adapter(exchanges)
    a.profile = replace(a.profile, channels=(1, *range(401, 433)))
    return a, transport


def test_temperature_units_and_physical_channel_are_preserved():
    a, transport = input_adapter(snapshot() + [("READ?", b"25.35")] + snapshot())
    frame = a.acquire(AcquisitionRequest("scalar", (428,), confirmed=True))
    m = frame.measurements[0]
    assert (m.channel, m.quantity, m.unit, m.values) == (428, "temperature", "°C", (25.35,))
    assert "curve" in m.metadata["conversion"]
    assert transport.history.count("READ?") == 1


@pytest.mark.parametrize(
    "raw,code",
    [
        (b"1,2", "parse_error"),
        (b"nan", "overload"),
        (b"9.9E37", "overload"),
        (b"-9.9E37", "overload"),
        (b"broken", "parse_error"),
    ],
)
def test_invalid_reply_is_not_retried_or_hidden_as_configuration_change(raw, code):
    a, transport = input_adapter(snapshot() + [("READ?", raw)])
    with pytest.raises(DeviceError) as caught:
        a.acquire(AcquisitionRequest(confirmed=True))
    assert caught.value.code == code
    assert transport.history[-1] == "READ?" and transport.history.count("READ?") == 1


def test_complete_valid_reply_with_changing_units_is_discarded():
    a, _ = input_adapter(snapshot() + [("READ?", b"25.35")] + snapshot(unit="F"))
    with pytest.raises(DeviceError) as caught:
        a.acquire(AcquisitionRequest(confirmed=True))
    assert caught.value.code == "configuration_changed"


@pytest.mark.parametrize(
    "options", [{"channels": ()}, {"trigger": "EXT"}, {"count": "2"}, {"format_on": True}]
)
def test_unsupported_scan_state_never_starts_or_writes(options):
    a, transport = input_adapter(snapshot(**options))
    with pytest.raises(DeviceError):
        a.acquire(AcquisitionRequest(confirmed=True))
    assert "READ?" not in transport.history
    assert all("?" in command for command in transport.history)


def test_no_implicit_acquisition_from_read_settings_or_unconfirmed_request():
    a, transport = input_adapter([])
    assert a.read_settings(("measurements",), 428)[0].error
    with pytest.raises(DeviceError) as caught:
        a.acquire(AcquisitionRequest())
    assert caught.value.code == "confirmation_required" and not transport.history


def test_wrong_physical_channel_never_starts_scan():
    a, transport = input_adapter(snapshot())
    with pytest.raises(DeviceError):
        a.acquire(AcquisitionRequest(channels=(427,), confirmed=True))
    assert "READ?" not in transport.history


def test_dynamic_inventory_is_query_only_and_never_exposes_empty_or_dmm_slots():
    exchanges = [
        (f"SYST:CTYP? {slot}", f"RIGOL TECHNOLOGIES,{model},SERIAL,1.0".encode())
        for slot, model in ((100, "0"), (200, "0"), (300, "0"), (400, "MC3132"), (500, "MC3065"))
    ]
    a, transport = adapter(exchanges + [("INST:DMM:INST?", b"1"), ("ROUT:SCAN?", b"(@428)")])
    a.refresh_capabilities()
    assert a.capabilities().channels == (1, *range(401, 433))
    assert a.capability_details["acquisition_channels"] == [428]
    assert all("?" in q for q in transport.history)
    with pytest.raises(DeviceError):
        a.capabilities_for_channel(1).get("function")


def test_temperature_write_targets_only_selected_channel_and_checks_error():
    a, transport = input_adapter(
        [
            ("FUNC? (@428)", b"TEMP"),
            ("FUNC? (@428)", b"TEMP"),
            ("TEMP:TRAN:TYPE? (@428)", b"THER"),
            ("TEMP:TRAN:THER:TYPE 10000,(@428)", b""),
            ("TEMP:TRAN:THER:TYPE? (@428)", b"+10000"),
            ("SYST:ERR?", b'0,"No error"'),
        ]
    )
    assert a.apply(DeviceCommand("thermistor_type", {"value": 10000}, 428, True)).outcome == "success"
    assert not any("427" in q for q in transport.history)


@pytest.mark.parametrize("scan", ["(@428,428)", "(@428:427)", "(@428:501)", "(@999)"])
def test_invalid_scan_write_is_blocked_before_transport(scan):
    a, transport = input_adapter([])
    with pytest.raises(DeviceError):
        a.apply(DeviceCommand("scan_list", {"value": scan}, confirmed=True))
    assert not transport.history


def test_scan_list_ranges_and_configuration_parser():
    assert scan_channels("(@401:403,428)") == (401, 402, 403, 428)
    assert measurement_kind("RES +1E4,+1E-4") == ("resistance", "ohm")
    with pytest.raises(DeviceError):
        measurement_kind("RES invalid")


def test_uncertain_write_is_never_replayed():
    a, transport = input_adapter([])

    def timeout(command):
        transport.history.append(command)
        raise DeviceError("timeout after write", "timeout")

    transport.write = timeout
    result = a.apply(DeviceCommand("function", {"value": "TEMP"}, 428, True))
    assert result.outcome == "uncertain"
    assert transport.history == ['FUNC "TEMP",(@428)']


def test_nineteen_channel_bank_keeps_each_nominal_and_value_in_order():
    channels = tuple(range(401, 420))
    replies = snapshot(channels)
    for i, (query, reply) in enumerate(replies):
        if query.startswith("CONF?") and int(query[-4:-1]) <= 416:
            replies[i] = (query, reply.replace(b"10000", b"5000"))
    values = tuple(20 + index / 10 for index in range(19))
    raw = ",".join(map(str, values)).encode()
    a, transport = input_adapter(replies + [("READ?", raw)] + replies)
    frame = a.acquire(AcquisitionRequest("scalar", channels, confirmed=True))
    assert tuple(m.channel for m in frame.measurements) == channels
    assert tuple(m.values[0] for m in frame.measurements) == values
    assert all(m.unit == "°C" for m in frame.measurements)
    assert all("THER,5000," in m.metadata["configuration"] for m in frame.measurements[:16])
    assert all("THER,10000," in m.metadata["configuration"] for m in frame.measurements[16:])
    assert transport.history.count("READ?") == 1


def test_one_open_input_rejects_entire_bank_without_replaying_scan():
    channels = tuple(range(401, 420))
    raw = ",".join(["25.0"] * 18 + ["9.9E37"]).encode()
    a, transport = input_adapter(snapshot(channels) + [("READ?", raw)])
    with pytest.raises(DeviceError) as exc:
        a.acquire(AcquisitionRequest("scalar", channels, confirmed=True))
    assert exc.value.code == "overload"
    assert transport.history[-1] == "READ?" and transport.history.count("READ?") == 1
