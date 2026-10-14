"""JW8105 metadata and capability declarations."""

from __future__ import annotations

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import Capability, CapabilitySet, Parameter
from lab_control.instruments.base import CommandSpec, Profile

OFFICIAL_PRODUCT_URL = "https://www.joinwit.com/pro-info/638.html"
OFFICIAL_DOWNLOAD_URL = "https://www.joinwit.com/down.html"
PROTOCOL_EVIDENCE = (
    "用户提供《JW8105 用户通信协议 V23.02.02》PDF，SHA256 "
    "C708FDB6522D482637B0ACB3D7C7A867299925D9646A40804275CEDA42FFD4B6；"
    "正文型号为 JW8105，PDF 元数据标题为 JW8103，停止位未说明。"
)

_WAVELENGTH = Parameter("number", "nm", 850.0, 1625.0, resolution=0.01)
_SAMPLE_RATE = Parameter("integer", "code", 0, 8)
_DISPLAY_WAVELENGTHS = Parameter("array", "nm", 850.0, 1625.0)


def _capabilities() -> tuple[Capability, ...]:
    return (
        Capability("power", readable=True, channel_scoped=True, source=OFFICIAL_PRODUCT_URL, section="0x0142"),
        Capability("display_power", readable=True, channel_scoped=True, source=OFFICIAL_PRODUCT_URL, section="0x0162"),
        Capability("wavelength", {"value": _WAVELENGTH}, True, True, True, False, source=OFFICIAL_PRODUCT_URL, section="0x0148/0x0146"),
        Capability("sampling_rate", {"value": _SAMPLE_RATE}, True, True, True, True, source=OFFICIAL_PRODUCT_URL, section="0x043C/0x0438"),
        Capability(
            "display_wavelengths",
            {"value": _DISPLAY_WAVELENGTHS},
            True,
            True,
            True,
            False,
            source=OFFICIAL_PRODUCT_URL,
            section="0x0730/0x0732",
        ),
        Capability("pdl", readable=True, channel_scoped=True, source=OFFICIAL_PRODUCT_URL, section="0x0440"),
    )


def capability_schema(_config: ConnectionConfig) -> CapabilitySet:
    return CapabilitySet("optical_power_meter", tuple(range(1, 9)), _capabilities())


def profile() -> Profile:
    commands = {cap.name: CommandSpec(capability=cap) for cap in _capabilities()}
    return Profile(
        adapter_id="joinwit.jw8105",
        manufacturer=("Joinwit", "嘉慧"),
        models=("JW8105",),
        category="optical_power_meter",
        channels=tuple(range(1, 9)),
        source=OFFICIAL_PRODUCT_URL,
        revision="V23.02.02 user-supplied protocol evidence; 115200 8-bit no parity; stop bit unspecified",
        commands=commands,
        status=("power", "wavelength", "sampling_rate"),
        # A text token is retained only for the generic MockTransport contract;
        # real serial/VISA sessions use the binary 0x0140 frame in adapter.py.
        identity_query="JW8105_CONNECT_FRAME",
        error_query="",
        acquisition="jw8105-power",
    )
