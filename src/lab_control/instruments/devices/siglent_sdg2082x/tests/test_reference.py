import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import DeviceCommand, DeviceError
from lab_control.instruments.devices.siglent_sdg2082x.adapter import Sdg2082xAdapter, sdg2082x
from lab_control.instruments.devices.siglent_sdg2082x.protocol import reference_source, reference_state
from lab_control.transport.base import MockTransport


@pytest.mark.parametrize("header", ["ROSC ", "ROSCILLATOR ", ""])
@pytest.mark.parametrize("source", ["INT", "EXT"])
@pytest.mark.parametrize("output", ["", ",10MOUT,ON", ",10MOUT,OFF"])
def test_reference_response(header, source, output):
    raw = (header + source + output + "\r\n").encode()
    assert reference_source(raw) == source
    assert reference_state(raw) == {"source": source, **({"10MOUT": output.split(",")[-1]} if output else {})}


@pytest.mark.parametrize(
    "raw", [b"", b"ROSC UNKNOWN", b"ROSC INT,10MOUT,BOGUS", b"ROSC EXT,10MOUT", b"ROSC INT,LOCK,ON"]
)
def test_invalid_reference_is_not_internal(raw):
    with pytest.raises(DeviceError):
        reference_source(raw)


@pytest.mark.parametrize("source", ["INT", "EXT"])
def test_global_reference_only_writes_source_and_checks_readback(source):
    transport = MockTransport([(f"ROSC {source}", None), ("ROSC?", f"ROSC {source},10MOUT,ON".encode())])
    transport.open()
    adapter = Sdg2082xAdapter(transport, sdg2082x(), ConnectionConfig("mock", "siglent.sdg2082x"))
    cap = adapter.capabilities().get("reference_clock")
    assert not cap.channel_scoped and cap.parameters["value"].choices == ("INT", "EXT")
    result = adapter.apply(DeviceCommand("reference_clock", {"value": source}, 1))
    assert result.outcome == "success" and result.readback == source
    assert transport.history == [f"ROSC {source}", "ROSC?"]
    with pytest.raises(DeviceError):
        adapter.apply(DeviceCommand("reference_clock", {"value": "AUTO"}))
    assert len(transport.history) == 2
