"""USB observations are not hardware identity or acquisition evidence."""

from lab_control.domain.connections import ConnectionConfig
from lab_control.instruments.common.windows_usb import enumerate_usb


def enumerate_devices():
    return enumerate_usb("16D0", "05B8")


class UsbDiscoveryProvider:
    name = "usb_pnp"

    def enumerate(self):
        # Local import avoids registry / discovery / session circular imports.
        from lab_control.discovery.service import DiscoveryCandidate

        candidates = []
        for row in enumerate_devices():
            instance = row["PNPDeviceID"]
            problem = row.get("ConfigManagerErrorCode")
            candidates.append(
                DiscoveryCandidate(
                    ConnectionConfig(
                        "USBPNP::" + instance,
                        "microchip.53100a",
                        "tserve",
                        timeout=10,
                        usb_instance_id=instance,
                    ),
                    self.name,
                    {
                        **row,
                        "display_name": "Microchip 53100A（身份待核验）",
                        "identity_evidence": "USB descriptor only; serial not verified",
                    },
                    error_code="driver_missing" if problem == 28 else "driver_error" if problem else "",
                    error=f"53100A USB 驱动未就绪（代码 {problem}）；请检查 TimeLab 配套驱动"
                    if problem
                    else "",
                )
            )
        return tuple(candidates)
