import ctypes
from types import SimpleNamespace

import pytest

from lab_control.domain.instruments import DeviceError
from lab_control.transport.visa import VisaRuntime, VisaTransport


@pytest.mark.parametrize("failure", [False, True])
def test_discovery_restores_resource_manager_settings_even_after_timeout(failure):
    attributes = {0x0FAF0001: 2000, 0x0FAF0002: 1}
    original = dict(attributes)

    def get_attribute(session, attribute, pointer):
        assert session == 7
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_size_t))[0] = attributes[attribute]

    def set_attribute(session, attribute, value):
        assert session == 7
        attributes[attribute] = value

    def listing(expression):
        assert expression == "TCPIP?*::INSTR"
        assert attributes == {0x0FAF0001: 3000, 0x0FAF0002: 6}
        if failure:
            raise DeviceError("injected discovery timeout", "timeout")
        return ("TCPIP0::192.0.2.2::inst0::INSTR",)

    manager = SimpleNamespace(
        session=7,
        visalib=SimpleNamespace(
            lib=SimpleNamespace(viGetAttribute=get_attribute), set_attribute=set_attribute
        ),
        list_resources=listing,
    )
    runtime = VisaRuntime(manager=manager)
    if failure:
        with pytest.raises(DeviceError):
            runtime.discover_lan()
    else:
        assert runtime.discover_lan() == ("TCPIP0::192.0.2.2::inst0::INSTR",)
    assert attributes == original


def test_unsupported_backend_does_not_open_or_probe_instruments():
    runtime = VisaRuntime(manager=SimpleNamespace(visalib=SimpleNamespace()))
    with pytest.raises(DeviceError, match="no R&S LAN discovery"):
        runtime.discover_lan()


@pytest.mark.parametrize("code", ["driver_missing", "connection_failed"])
def test_open_retains_runtime_error_classification_without_retry(code):
    attempts = []

    def unavailable(resource, **kwargs):
        attempts.append(resource)
        if code == "driver_missing":
            raise DeviceError("VISA runtime unavailable", code)
        raise RuntimeError("VI_ERROR_RSRC_NFOUND")

    transport = VisaTransport(SimpleNamespace(open=unavailable), "TCPIP0::192.0.2.10::INSTR")
    with pytest.raises(DeviceError) as failure:
        transport.open()
    assert failure.value.code == code
    assert attempts == [transport.resource] and transport.handle is None
