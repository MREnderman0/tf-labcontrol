from __future__ import annotations

from types import SimpleNamespace

from lab_control.instruments.devices.coremorrow_xmt import discovery


def test_only_verified_usb_bridge_is_published(monkeypatch):
    monkeypatch.setattr(
        discovery,
        "enumerate_devices",
        lambda: (
            SimpleNamespace(
                device="COM6",
                vid=0x0483,
                pid=0x0002,
                serial_number="00000000001A",
                description="USB Serial Device",
                manufacturer="STMicroelectronics",
                location="1-1.4.3",
            ),
            SimpleNamespace(
                device="COM8",
                vid=0x0403,
                pid=0x6001,
                serial_number="FTDI-OTHER",
                description="FT232R USB UART",
                manufacturer="FTDI",
                location="1-1.4.4",
            ),
        ),
    )

    (candidate,) = discovery.SerialDiscoveryProvider().enumerate()
    assert candidate.config.resource == "COM6"
    assert candidate.config.adapter == "coremorrow.xmt"
    assert candidate.config.transport == "serial"
    assert candidate.config.usb_instance_id == "VID_0483&PID_0002\\00000000001A"
    assert candidate.descriptor["identity_evidence"].startswith("USB bridge signature")
