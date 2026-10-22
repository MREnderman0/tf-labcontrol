import ctypes as ct

import numpy as np
import pytest

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError, ValidationError

from ..adapter import PicoAdapter, StreamingInfo, TriggerInfo


class Symbol:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


class FakeLibrary:
    def __init__(self, functions):
        for name, callback in functions.items():
            setattr(self, name, Symbol(callback))


def pointer_write(pointer, dtype, value):
    ct.cast(pointer, ct.POINTER(dtype))[0] = value


def test_pico_model_ranges_coupling_offset_and_trigger_are_consistent():
    calls = []

    def limits(handle, resolution, low, high):
        pointer_write(low, ct.c_int16, -32767)
        pointer_write(high, ct.c_int16, 32767)
        return 0

    fake = FakeLibrary(
        {
            "ps6000aOpenUnit": lambda *args: 0,
            "ps6000aSetChannelOn": lambda *args: calls.append(("channel", args)) or 0,
            "ps6000aGetAdcLimits": limits,
            "ps6000aSetSimpleTrigger": lambda *args: calls.append(("trigger", args)) or 0,
            "ps6000aStop": lambda *args: 0,
            "ps6000aCloseUnit": lambda *args: 0,
        }
    )
    device = PicoAdapter(ConnectionConfig("PICO::SERIAL", "pico.6404e", "pico"), fake)
    try:
        for span, coupling, offset in (("50", "DC", 0), ("10", "DC50", 0), ("0.5", "DC", 2)):
            with pytest.raises(ValidationError):
                device.apply(
                    DeviceCommand(
                        "channel", {"enabled": True, "range": span, "coupling": coupling, "offset": offset}
                    )
                )
        assert not calls
        device.apply(
            DeviceCommand("channel", {"enabled": True, "range": "0.5", "coupling": "DC50", "offset": 0.05})
        )
        device.apply(
            DeviceCommand(
                "trigger", {"enabled": True, "threshold": 0.2, "direction": "RISING", "timeout_us": 1000}
            )
        )
        assert calls[-1][0] == "trigger" and calls[-1][1][3] == 16384
        count = len(calls)
        reading = device.read_settings(("channel",))[0]
        assert reading.source == "session_cache" and reading.value["range"] == "0.5"
        assert device.read_settings(("trigger",))[0].value["threshold"] == 0.2
        assert len(calls) == count  # No invented SDK getter.
        with pytest.raises(ValidationError):
            device.apply(
                DeviceCommand(
                    "trigger", {"enabled": True, "threshold": 0.5, "direction": "RISING", "timeout_us": 1000}
                )
            )
    finally:
        device.close()


@pytest.mark.parametrize("kind", ["waveform", "streaming"])
def test_pico_block_buffer_lifecycle_and_scaling(kind):
    calls, buffers = [], {}

    def open_unit(handle, serial, resolution):
        pointer_write(handle, ct.c_int16, 7)
        return 0

    def unit_info(handle, buffer, length, required, code):
        buffer.value = {3: b"6404E", 4: b"PICO-SERIAL", 9: b"1.0"}[code]
        return 0

    def limits(handle, resolution, low, high):
        pointer_write(low, ct.c_int16, -32767)
        pointer_write(high, ct.c_int16, 32767)
        return 0

    def data_buffer(handle, channel, buffer, count, dtype, segment, ratio, action):
        calls.append("register" if buffer is not None else "release")
        if buffer is not None:
            buffers[channel] = buffer
        return 0

    def timebase(handle, index, samples, interval, maximum, segment):
        pointer_write(interval, ct.c_double, 2)
        pointer_write(maximum, ct.c_uint64, 10000)
        return 0

    def ready(handle, pointer):
        pointer_write(pointer, ct.c_int16, 1)
        return 0

    def values(handle, start, count, ratio, mode, segment, overflow):
        for buffer in buffers.values():
            for index in range(len(buffer)):
                buffer[index] = 16384
        return 0

    stream_calls = 0

    def stream_values(handle, infos, count, trigger):
        nonlocal stream_calls
        values(handle, 0, 0, 0, 0, 0, 0)
        for info in infos:
            info.count = 4
            info.start = stream_calls * 4
        stream_calls += 1
        return 0

    fake = FakeLibrary(
        {
            "ps6000aOpenUnit": open_unit,
            "ps6000aGetUnitInfo": unit_info,
            "ps6000aSetChannelOn": lambda *args: 0,
            "ps6000aGetAdcLimits": limits,
            "ps6000aSetDataBuffer": data_buffer,
            "ps6000aGetTimebase": timebase,
            "ps6000aRunBlock": lambda *args: calls.append("run") or 0,
            "ps6000aIsReady": ready,
            "ps6000aGetValues": values,
            "ps6000aRunStreaming": lambda *args: calls.append("run") or 0,
            "ps6000aGetStreamingLatestValues": stream_values,
            "ps6000aStop": lambda *args: calls.append("stop") or 0,
            "ps6000aCloseUnit": lambda *args: calls.append("close") or 0,
        }
    )
    device = PicoAdapter(ConnectionConfig("PICO::PICO-SERIAL", "pico.6404e", "pico"), fake)
    device.identify()
    device.apply(DeviceCommand("channel", {"enabled": True, "range": "1", "coupling": "DC", "offset": 0}))
    batch = device.acquire(AcquisitionRequest(kind, points=8, initiate=True, confirmed=True))
    np.testing.assert_allclose(
        batch.measurements[0].axis, np.arange(8) * (2e-9 if kind == "waveform" else 1e-6)
    )
    np.testing.assert_allclose(batch.measurements[0].values, np.full(8, 16384 / 32767))
    assert calls.index("stop") < calls.index("release") and not device.buffers
    device.close()
    assert calls[-1] == "close"
    assert ct.sizeof(StreamingInfo) == 30 and ct.sizeof(TriggerInfo) == 12


def test_pico_registration_failure_releases_and_close_survives_stop_error():
    calls = []
    fail_stop = False

    def buffer(handle, channel, pointer, *args):
        calls.append("register" if pointer is not None else "release")
        return 123 if pointer is not None else 0

    fake = FakeLibrary(
        {
            "ps6000aOpenUnit": lambda *args: 0,
            "ps6000aSetDataBuffer": buffer,
            "ps6000aStop": lambda *args: 123 if fail_stop else calls.append("stop") or 0,
            "ps6000aCloseUnit": lambda *args: calls.append("close") or 0,
        }
    )
    device = PicoAdapter(ConnectionConfig("PICO::SERIAL", "pico.6404e", "pico"), fake)
    from lab_control.domain.instruments import DeviceIdentity

    device.identity = DeviceIdentity("Pico", "6404E")
    device.settings = {1: {"enabled": True, "range": "1", "offset": 0}}
    with pytest.raises(DeviceError):
        device.acquire(AcquisitionRequest("waveform", points=4, initiate=True, confirmed=True))
    assert calls == ["register", "stop", "release"] and not device.buffers
    fail_stop = True
    with pytest.raises(DeviceError):
        device.close()
    assert calls[-1] == "close" and not device.opened
