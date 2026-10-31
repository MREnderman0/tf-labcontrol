"""Independent K+K FXE compatibility module; import is hardware-free."""

from pathlib import Path

from lab_control.domain.processing import ProcessingProfile
from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import KpluskFxeAdapter, create
from .discovery import dhcp_discovery_config, normalize_discovery_config
from .profile import capability_schema, profile


def _readonly_check(*args, **kwargs):
    from .hardware.readonly import verify

    return verify(*args, **kwargs)


DEFINITION = DeviceDefinition(
    profile(),
    KpluskFxeAdapter,
    DeviceDocumentation(
        "K+K FXE / FXE2",
        "measurement-analyzers/kplusk-fxe",
        "Ethernet（FXE2 使用 DHCP，已登记身份随租约更新地址）",
        "K+K Library 21.2.2 / Python wrapper 2.1",
    ),
    Path(__file__).parent,
    deployment_components=("kplusk-library-win", "kplusk-python-wrapper"),
    factory=create,
    capability_schema=capability_schema,
    identity_requires_adapter=True,
    discovery_config=normalize_discovery_config,
    discovery_lan_config=dhcp_discovery_config,
    readonly_check=_readonly_check,
    offline_processing=ProcessingProfile("scalar"),
)

__all__ = ["DEFINITION", "KpluskFxeAdapter"]
