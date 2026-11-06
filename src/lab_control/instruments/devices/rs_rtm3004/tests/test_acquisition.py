import numpy as np
import pytest

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceError, DeviceIdentity, Unsupported
from lab_control.instruments.devices.rs_rtm3004.protocol import Header, waveform
from lab_control.instruments.registry import build_scpi_adapter
from lab_control.transport.base import MockTransport, ieee_block

HEADER = b"-1.000000000E-3,1.000000000E-3,3,1"
VALUES = np.array([0.125, -0.25, 0.5], dtype="<f4")


class Wire(MockTransport):
    def query_binary(self, command, max_bytes):
        assert max_bytes == 4_000_000 * 4
        return self.query(command)


def make_device(monkeypatch, *, changed=False, raw=None, post=HEADER, enabled=True, running=False):
    exchanges = [
        ("ACQ:STAT?", b"RUN" if running else b"BRE"),
        ("FORM?", b"REAL,32"),
        ("FORM:BORD?", b"LSBF"),
        ("CHAN1:DATA:HEAD?", HEADER),
        ("CHAN1:DATA:XOR?", b"-0.001"),
        ("CHAN1:DATA:XINC?", b"0.001"),
        ("CHAN1:DATA?", ieee_block(VALUES.tobytes()) if raw is None else raw),
        ("CHAN1:DATA:HEAD?", post),
    ]
    if running and float(post.split(b",")[2]) > 3:
        exchanges.append(("CHAN1:DATA:XINC?", b"0.001"))
    elif running and float(post.split(b",")[2]) < 3:
        exchanges.extend([("CHAN1:DATA:XOR?", b"-0.001"), ("CHAN1:DATA:XINC?", b"0.001")])
    exchanges.extend(
        [("ACQ:STAT?", b"RUN" if running else b"BRE"), ("FORM?", b"REAL,32"), ("FORM:BORD?", b"LSBF")]
    )
    wire = Wire(exchanges)
    wire.open()
    device = build_scpi_adapter(ConnectionConfig("mock://rtm", "rs.rtm3004"), wire)
    device.identity = DeviceIdentity("Rohde&Schwarz", "RTM3004", "SYNTHETIC", "01.400")
    settings = {
        "1.decimation": "SAMP",
        "1.acquisition_mode": "REFR",
        "1.channel_enabled": enabled,
        "1.scale": 0.1,
        "1.offset": 0.3,
        "1.vertical_position": 2.0,
        "1.horizontal_divisions": 12,
        "1.time_position": 0.0,
        "1.time_reference": 50.0,
        "1.timebase": 0.001 / 6,
    }
    after = dict(settings)
    if changed:
        after["1.scale"] = 0.2
    snapshots = iter([(settings, {}), (after, dict.fromkeys(after, "2026-01-01T00:00:00Z"))])
    monkeypatch.setattr(device, "snapshot", lambda: next(snapshots))
    return device, wire


def test_full_binary_frame_and_twelve_division_metadata(monkeypatch):
    device, wire = make_device(monkeypatch)
    batch = device.acquire(AcquisitionRequest("waveform", (1,)))
    measured = batch.measurements[0]
    np.testing.assert_array_equal(measured.values, VALUES)
    np.testing.assert_allclose(measured.axis, [-0.001, 0, 0.001])
    assert batch.validation == "full" and batch.settings_timestamps
    assert batch.performance["instrument_running"] is False
    assert measured.metadata["scope_display"]["horizontal_divisions"] == 12
    assert measured.metadata["scope_display"]["reference_value"] == pytest.approx(0.1)
    assert all("?" in command for command in wire.history)
    assert not wire.exchanges


def test_formal_session_added_configuration_does_not_become_instrument_reading(monkeypatch, tmp_path):
    from lab_control.application.runtime import ApplicationRuntime

    device, wire = make_device(monkeypatch)
    config = ConnectionConfig("mock://rtm-session", "rs.rtm3004", "mock")
    with ApplicationRuntime((config,), root=tmp_path, hardware=False) as app:
        key = app.add(config)
        app.connect(key).result()
        session = app.sessions.sessions[key]
        session.adapter = device
        batch = app.sessions.submit(key, "acquire", AcquisitionRequest("waveform", (1,))).result(5)
        assert batch.configuration["_session_configuration_version"] == 0
        assert session.state == "connected"
        assert session.settings[1, "scale"].value == 0.1
        assert not wire.exchanges


@pytest.mark.parametrize(
    "kwargs,code",
    [
        ({"changed": True}, "configuration_changed"),
        ({"post": b"-2E-3,2E-3,3,1"}, "configuration_changed"),
        (
            {"post": b"-1E-3,2E-3,4,1", "raw": ieee_block(np.zeros(4, dtype="<f4").tobytes())},
            "configuration_changed",
        ),
        ({"post": b"-1E-3,2E-3,4,1", "raw": b"#212abcd"}, "parse_error"),
        ({"raw": b"#212abcd"}, "parse_error"),
        ({"raw": ieee_block(np.array([0, np.nan, 1], dtype="<f4").tobytes())}, "parse_error"),
    ],
)
def test_invalid_frame_is_rejected_without_replay(monkeypatch, kwargs, code):
    device, wire = make_device(monkeypatch, **kwargs)
    with pytest.raises(DeviceError) as caught:
        device.acquire(AcquisitionRequest("waveform", (1,)))
    assert caught.value.code == code
    assert wire.history.count("CHAN1:DATA?") == 1
    if kwargs.get("changed"):
        assert (
            "1.scale" in str(caught.value) and "before" in str(caught.value) and "after" in str(caught.value)
        )
    if "post" in kwargs and code == "configuration_changed":
        assert "header changed" in str(caught.value) and "->" in str(caught.value)


def test_disabled_and_explicit_memory_requests_do_not_write(monkeypatch):
    device, wire = make_device(monkeypatch, enabled=False)
    with pytest.raises(Unsupported):
        device.acquire(AcquisitionRequest("waveform", (1,)))
    assert not wire.history
    with pytest.raises(Unsupported):
        device.acquire(AcquisitionRequest("waveform", (1,), waveform_extent="memory", confirmed=True))
    assert not wire.history


def test_running_visible_prefix_uses_block_count_only_on_verified_unchanged_time_grid(monkeypatch):
    raw = ieee_block(np.array([0.1, 0.2, 0.3, 0.4], dtype="<f4").tobytes())
    device, wire = make_device(monkeypatch, raw=raw, post=b"-1E-3,3E-3,5,1", running=True)
    batch = device.acquire(AcquisitionRequest("waveform", (1,), consistency="stream", context_id="growing"))
    assert batch.performance["instrument_running"]
    measured = batch.measurements[0]
    assert measured.metadata["growing_prefix"]
    np.testing.assert_allclose(measured.axis, [-0.001, 0, 0.001, 0.002])
    assert len(measured.values) == 4 and not wire.exchanges
    assert batch.validation == "full" and device._context_cache is None


@pytest.mark.parametrize(
    "after, count, after_step",
    [
        (Header(-0.002, 0.002, 5, 1e-12), 4, 0.001),
        (Header(-0.001, 0.004, 5, 1e-12), 4, 0.001),
        (Header(-0.001, 0.003, 5, 1e-12), 6, 0.001),
        (Header(-0.001, 0.003, 5, 1e-12), 4, 0.002),
        (Header(-0.001, 0, 2, 1e-12), 2, 0.001),
    ],
)
def test_growing_prefix_rejects_grid_changes_rollover_and_unbracketed_counts(after, count, after_step):
    from ..protocol import growing_header

    assert (
        growing_header(
            Header(-0.001, 0.001, 3, 1e-12), after, np.zeros(count), -0.001, 0.001, after_step, 1e-12
        )
        is None
    )


@pytest.mark.parametrize("count", [2, 3])
def test_rollover_requires_exact_endpoint_record_and_full_configuration_check(monkeypatch, count):
    raw = ieee_block(np.arange(count, dtype="<f4").tobytes())
    device, wire = make_device(monkeypatch, raw=raw, post=b"-1E-3,0,2,1", running=True)
    batch = device.acquire(AcquisitionRequest("waveform", (1,), consistency="stream", context_id="roll"))
    measured = batch.measurements[0]
    assert measured.metadata["sweep_rollover"] and not measured.metadata["growing_prefix"]
    assert len(measured.values) == count and not wire.exchanges
    assert batch.validation == "full" and device._context_cache is None
    np.testing.assert_allclose(measured.axis, np.arange(count) * 0.001 - 0.001)


@pytest.mark.parametrize("change", ["count", "origin", "increment", "header", "direction"])
def test_rollover_rejects_ambiguous_count_or_changed_time_grid(change):
    from ..protocol import rollover_header

    before, after = Header(-0.001, 0.003, 5, 1e-12), Header(-0.001, 0, 2, 1e-12)
    values, origin, increment = np.zeros(5), -0.001, 0.001
    if change == "count":
        values = np.zeros(3)
    elif change == "origin":
        origin = -0.002
    elif change == "increment":
        increment = 0.002
    elif change == "header":
        after = Header(-0.001, 0.001, 2, 1e-12)
    else:
        before, after = after, before
    assert rollover_header(before, after, values, -0.001, 0.001, origin, increment, 1e-12) is None


def test_rollover_does_not_hide_configuration_change_or_corrupt_payload(monkeypatch):
    for kwargs, expected in (
        ({"changed": True}, "configuration_changed"),
        ({"raw": b"#212bad"}, "parse_error"),
    ):
        device, wire = make_device(monkeypatch, post=b"-1E-3,0,2,1", running=True, **kwargs)
        with pytest.raises(DeviceError) as caught:
            device.acquire(AcquisitionRequest("waveform", (1,)))
        assert caught.value.code == expected and wire.history.count("CHAN1:DATA?") == 1


@pytest.mark.parametrize("raw", [b"0,1,3.5,1", b"nan,1,3,1", b"0,1,4000001,1", b"0,1,3,2", b"0,0,3,1"])
def test_header_rejects_invalid_or_unbounded_data(raw):
    with pytest.raises(DeviceError):
        Header.parse(raw)


def test_axis_mismatch_is_not_a_valid_frame():
    with pytest.raises(DeviceError, match="coordinates"):
        waveform(ieee_block(VALUES.tobytes()), Header.parse(HEADER), 0, 0.001, 1)
