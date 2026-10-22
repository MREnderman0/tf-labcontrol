import ctypes as ct

import pytest

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError, ValidationError

from ..adapter import PowerMeterAdapter


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


def test_sdk_initialization_error_is_not_retried():
    calls = []

    def fail(*args):
        calls.append(args)
        return -123

    with pytest.raises(DeviceError, match="returned"):
        PowerMeterAdapter(
            ConnectionConfig("USB::pm", "thorlabs.pm100d", "tlpmx"), FakeLibrary({"TLPMX_init": fail})
        )
    assert len(calls) == 1
