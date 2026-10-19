"""Read-only discovery for the observed CoreMorrow E00 USB serial bridge.

Enumeration only reads Windows' serial-port descriptors.  The discovery
service performs the bounded, model-owned protocol identity query after it has
acquired the normal process/resource leases; no settings are written here.
"""

from __future__ import annotations

from lab_control.application.config import ConnectionConfig

# The attached E00 enumerates as an ST USB serial device.  Keep this allowlist
# deliberately narrow: a generic COM port must remain unconfigured unless its
# bridge signature is one we have verified against the XMT wire protocol.
SUPPORTED_USB_IDS = frozenset({(0x0483, 0x0002)})


def enumerate_devices():
    from serial.tools.list_ports import comports

    return tuple(port for port in comports() if (port.vid, port.pid) in SUPPORTED_USB_IDS)


class SerialDiscoveryProvider:
    """Publish candidate configs; the common service owns all hardware I/O."""

    name = "coremorrow_usb_serial"

    def enumerate(self):
        # Local import avoids a discovery/registry cycle at package import time.
        from lab_control.discovery.service import DiscoveryCandidate

        candidates = []
        for port in enumerate_devices():
            if (port.vid, port.pid) not in SUPPORTED_USB_IDS:
                continue
            usb_instance_id = ""
            if port.serial_number and port.vid is not None and port.pid is not None:
                usb_instance_id = f"VID_{port.vid:04X}&PID_{port.pid:04X}\\{port.serial_number}"
            candidates.append(
                DiscoveryCandidate(
                    ConnectionConfig(
                        port.device,
                        adapter="coremorrow.xmt",
                        transport="serial",
                        baud_rate=9600,
                        timeout=3.0,
                        usb_instance_id=usb_instance_id,
                    ),
                    self.name,
                    {
                        "description": port.description,
                        "manufacturer": port.manufacturer,
                        "vid": port.vid,
                        "pid": port.pid,
                        "usb_serial": port.serial_number,
                        "usb_instance_id": usb_instance_id,
                        "location": port.location,
                        "identity_evidence": "USB bridge signature; XMT protocol read-only probe required",
                    },
                )
            )
        return tuple(candidates)
