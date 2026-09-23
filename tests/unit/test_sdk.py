import ctypes as ct

import numpy as np
import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError, ValidationError
from lab_control.instruments.optical import PowerMeterAdapter
from lab_control.instruments.pico import PicoAdapter, StreamingInfo, TriggerInfo
from lab_control.transport.sdk import VendorLibrary


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


def test_tlpmx_initialization_never_resets_and_checks_sensor_bounds():
    calls = []

    def init(resource, query, reset, handle):
        calls.append((resource, query, reset))
        pointer_write(handle, ct.c_uint32, 42)
        return 0

    def identity(handle, manufacturer, model, serial, firmware):
        for buffer, value in zip(
            (manufacturer, model, serial, firmware), (b"Thorlabs", b"PM100D", b"SER", b"1")
        ):
            buffer.value = value
        return 0

    def sensor(handle, name, serial, message, kind, subtype, flags, channel):
        name.value, serial.value = b"S154C", b"SENSOR"
        return 0

    def wavelength(handle, attribute, result, channel):
        pointer_write(result, ct.c_double, {0: 1550, 1: 800, 2: 1700}[attribute])
        return 0

    def power(handle, result, channel):
        pointer_write(result, ct.c_double, 0.001)
        return 0

    def unit(handle, result, channel):
        pointer_write(result, ct.c_int16, 0)
        return 0

    fake = FakeLibrary(
        {
            "TLPMX_init": init,
            "TLPMX_identificationQuery": identity,
            "TLPMX_getSensorInfo": sensor,
            "TLPMX_getWavelength": wavelength,
            "TLPMX_measPower": power,
            "TLPMX_getPowerUnit": unit,
            "TLPMX_close": lambda handle: calls.append("closed") or 0,
        }
    )
    device = PowerMeterAdapter(ConnectionConfig("USB::pm", "thorlabs.pm100d", "tlpmx"), fake)
    assert calls[0] == (b"USB::pm", 1, 0)
    assert device.identify().model == "PM100D"
    device.refresh_capabilities()
    bounds = device.capabilities().get("wavelength").parameters["value"]
    assert (bounds.minimum, bounds.maximum, bounds.range_source) == (800, 1700, "instrument")
    assert device.capability_details["sensor"]["model"] == "S154C"
    wavelength_reading, sensor_reading, range_reading = device.read_settings(
        ("wavelength", "sensor", "range")
    )
    assert wavelength_reading.value == 1550 and wavelength_reading.source == "instrument"
    assert sensor_reading.source == "instrument"
    assert range_reading.source == "unavailable" and range_reading.value is None
    with pytest.raises(ValidationError):
        device.apply(DeviceCommand("wavelength", {"value": 700}))
    batch = device.acquire(AcquisitionRequest())
    assert batch.measurements[0].values == [0.001] and batch.measurements[0].unit == "W"
    fake.TLPMX_getWavelength.callback = lambda *args: -123
    with pytest.raises(DeviceError):
        device.refresh_capabilities()
    stale = next(c for c in device.capabilities().operations if c.name == "wavelength")
    assert not stale.available and stale.parameters["value"].maximum == 1700
    assert stale.parameters["value"].range_source == "unavailable"
    device.close()
    device.close()
    assert calls.count("closed") == 1


def test_missing_dll_and_error_code():
    with pytest.raises(DeviceError, match="absolute SDK"):
        VendorLibrary("no-such-driver.dll")
    fake = FakeLibrary({"TLPMX_init": lambda *args: -123})
    with pytest.raises(DeviceError, match="returned"):
        PowerMeterAdapter(ConnectionConfig("USB::pm", "thorlabs.pm100d", "tlpmx"), fake)


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


def test_legacy_pm100d_has_no_tlpmx_channel_argument_and_no_reset():
    calls = []

    def init(resource, query, reset, handle):
        calls.append(("init", reset))
        return 0

    def sensor(handle, name, serial, message, kind, subtype, flags):
        name.value, serial.value = b"S154C", b"LEGACY"
        return 0

    def wavelength(handle, attribute, result):
        pointer_write(result, ct.c_double, {0: 1550, 1: 800, 2: 1700}[attribute])
        return 0

    fake = FakeLibrary(
        {
            "PM100D_init": init,
            "PM100D_getSensorInfo": sensor,
            "PM100D_getWavelength": wavelength,
            "PM100D_setWavelength": lambda handle, value: calls.append(("wavelength", value)) or 0,
            "PM100D_close": lambda handle: 0,
        }
    )
    device = PowerMeterAdapter(ConnectionConfig("USB::legacy", "thorlabs.pm100d", "pm100d"), fake)
    assert device.apply(DeviceCommand("wavelength", {"value": 1550})).outcome == "success"
    assert calls == [("init", 0), ("wavelength", 1550)]
    device.close()
