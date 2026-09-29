from dataclasses import replace

import numpy as np
import pytest

from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError
from lab_control.instruments.registry import match
from lab_control.transport.base import ieee_block

from ..protocol import Preamble, waveform
from .fake import adapter


def ready():
    obj, peer = adapter()
    obj.refresh_capabilities()
    obj.prepare_acquisition()
    return obj, peer


def test_channel_receive_time_is_captured_before_post_descriptor_read(monkeypatch):
    obj, peer = ready()
    monkeypatch.setattr(
        "lab_control.instruments.devices.rigol_dho924.adapter.utc_now",
        lambda: "after:" + peer.history[-1],
    )
    batch = obj.acquire(AcquisitionRequest("waveform", (1, 2)))
    assert all(m.metadata["received_at"] == "after::WAV:DATA?" for m in batch.measurements)
    assert all(m.metadata["trigger_time_source"] == "unavailable" for m in batch.measurements)


def test_identity_exact_and_readonly_has_no_setting_writes():
    obj, peer = adapter()
    obj.refresh_capabilities()
    assert all("?" in q for q in peer.history)
    assert match(obj.identity) == "rigol.dho924"
    peer.model = "DHO924S"
    with pytest.raises(DeviceError, match="Expected"):
        obj.identify()


def test_stable_frames_keep_original_time_and_only_waveform_queries():
    obj, peer = ready()
    req = AcquisitionRequest("waveform", (1, 2, 3, 4), consistency="stream", context_id="run1")
    first = obj.acquire(req)
    peer.history.clear()
    second = obj.acquire(req)
    assert second.validation == "cached" and first.settings_timestamps == second.settings_timestamps
    assert [q for q in peer.history if "?" in q] == [":WAV:PRE?", ":WAV:DATA?", ":WAV:PRE?"] * 4
    assert second.performance["new_acquisitions"] is None
    assert all(m.metadata["waveform_extent"] == "screen" for m in second.measurements)
    obj.invalidate_acquisition_context("explicit_refresh")
    assert obj.acquire(req).validation == "full"
    assert obj.acquire(replace(req, context_id="run2")).validation == "full"


def test_corrupt_and_changed_descriptors_do_not_return_frames():
    obj, peer = ready()
    req = AcquisitionRequest("waveform", consistency="live", context_id="x")
    obj.acquire(req)
    peer.change_pre = True
    with pytest.raises(DeviceError) as error:
        obj.acquire(req)
    assert error.value.code == "configuration_changed" and obj._context_cache is None
    peer.change_pre = False
    peer.state[":ACQ:TYPE"] = "ULTR"
    with pytest.raises(DeviceError, match="超快分段采集.*ULTR"):
        obj.acquire(req)


def test_controls_ranges_isolation_and_no_replay():
    obj, peer = ready()
    before = peer.state[":CHAN2:OFFS"]
    assert obj.apply(DeviceCommand("offset", {"value": 0.01}, 1)).outcome == "success"
    assert peer.state[":CHAN2:OFFS"] == before
    with pytest.raises(DeviceError):
        obj.apply(DeviceCommand("record_length", {"value": 2500}))
    peer.fail = ":CHAN1:OFFS 0.02"
    assert obj.apply(DeviceCommand("offset", {"value": 0.02}, 1)).outcome == "uncertain"
    assert peer.history.count(peer.fail) == 1
    obj._settings["1.probe_attenuation"] = 10
    assert obj.capabilities_for_channel(1).get("scale").parameters["value"].minimum == 0.002


def test_completion_polls_zero_until_one_without_rewriting():
    obj, peer = ready()
    original = peer.query
    calls = []

    def delayed(payload):
        if payload == "*OPC?":
            calls.append(payload)
            if len(calls) < 3:
                return b"0\n"
        return original(payload)

    peer.query = delayed
    result = obj.apply(DeviceCommand("offset", {"value": 0.01}))
    assert result.outcome == "success" and len(calls) == 3
    assert peer.history.count(":CHAN1:OFFS 0.01") == 1


def test_disabled_channels_and_memory_budget_prevent_wave_query():
    obj, peer = ready()
    peer.state[":CHAN2:DISP"] = 0
    peer.history.clear()
    with pytest.raises(DeviceError, match="disabled"):
        obj.acquire(AcquisitionRequest("waveform", (2,)))
    assert ":WAV:DATA?" not in peer.history
    assert ":STOP" not in peer.history
    peer.state[":ACQ:MDEP"] = 50_000_000
    with pytest.raises(DeviceError, match="预算"):
        obj.acquire(AcquisitionRequest("waveform", confirmed=True, waveform_extent="memory"))
    assert ":WAV:DATA?" not in peer.history


def test_memory_restores_running_and_transfer_but_does_not_export():
    obj, peer = ready()
    before = {k: v for k, v in peer.state.items() if k.startswith(":WAV:")}
    batch = obj.acquire(AcquisitionRequest("waveform", confirmed=True, waveform_extent="memory"))
    assert len(batch.measurements[0].values) == 10000
    assert batch.measurements[0].metadata["waveform_extent"] == "memory"
    assert {k: v for k, v in peer.state.items() if k.startswith(":WAV:")} == before
    assert peer.state[":TRIG:STAT"] == "AUTO"
    assert peer.history.count(":STOP") == 1 and peer.history.count(":RUN") == 1


def test_memory_restore_failure_reports_no_success():
    obj, peer = ready()
    peer.fail = ":WAV:FORM WORD"
    with pytest.raises(DeviceError, match="未自动重放"):
        obj.acquire(AcquisitionRequest("waveform", confirmed=True, waveform_extent="memory"))
    assert (
        peer.state[":TRIG:STAT"] == "STOP" and peer.history.count(peer.fail) == 2
    )  # preparation + failed restore


@pytest.mark.parametrize("field,value", [(0, "2"), (2, "0"), (4, "nan"), (7, "-1"), (6, "1")])
def test_preamble_rejects_invalid_values(field, value):
    fields = "1,0,3,1,1e-9,-1e-9,0,0.001,10,32768".split(",")
    fields[field] = value
    if field == 0:
        pre = Preamble.parse(",".join(fields).encode())
        with pytest.raises(DeviceError):
            waveform(ieee_block(b"abcdef"), pre, 1, "V")
    else:
        with pytest.raises(DeviceError):
            Preamble.parse(",".join(fields).encode())


def test_word_scaling_and_time_axis_are_not_display_transformed():
    pre = Preamble.parse(b"1,0,3,1,1e-9,-1e-9,0,0.001,10,32768")
    raw = np.array([32767, 32768, 32769], dtype="<u2")
    m = waveform(ieee_block(raw.tobytes()) + b"\n", pre, 2, "V")
    np.testing.assert_allclose(m.values, [-0.011, -0.010, -0.009])
    np.testing.assert_allclose(m.axis, [-1e-9, 0, 1e-9])
    with pytest.raises(DeviceError, match="length"):
        waveform(ieee_block(b"12"), pre, 1, "V")


def test_descriptor_maps_settings_with_decimal_precision_and_bounded_origin_jitter():
    raw = b"1,0,1000,1,2.000000E-8,-1.000000E-5,0.000000,6.6667E-06,3750,32768"
    pre = Preamble.parse(raw)
    settings = dict(scale=0.05, offset=0.025, timebase=2e-6, position=0.0)
    pre.check_screen(raw, **settings)
    replace(pre, x_origin=pre.x_origin + pre.x_increment * 0.5).check_screen(raw, **settings)
    for bad in (
        replace(pre, x_origin=pre.x_origin + pre.x_increment * 2),
        replace(pre, y_origin=0),
        replace(pre, y_increment=pre.y_increment * 2),
    ):
        with pytest.raises(DeviceError) as failure:
            bad.check_screen(raw, **settings)
        assert failure.value.code == "configuration_changed"


def test_disabled_channel_write_is_rejected_before_setting_command():
    obj, peer = ready()
    peer.state[":CHAN2:DISP"] = 0
    obj.invalidate_acquisition_context("explicit_refresh")
    with pytest.raises(DeviceError, match="开启"):
        obj.apply(DeviceCommand("scale", {"value": 0.1}, 2))
    assert ":CHAN2:SCAL 0.1" not in peer.history
