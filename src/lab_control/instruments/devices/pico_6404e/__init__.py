"""PicoScope 6404E: pure SDK metadata with explicit connection factories."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation, DeviceModel

from .adapter import SOURCE, PicoAdapter, enumerate_pico


def create_adapter(config):
    from dataclasses import replace

    from lab_control.application.environment import find_sdk
    from lab_control.domain.instruments import Unsupported

    if config.transport not in ("pico",):
        raise Unsupported("Unsupported transport for pico.6404e")
    if not config.sdk_path:
        config = replace(config, sdk_path=find_sdk(config.transport))
    return PicoAdapter(config)


DEFINITION = DeviceDefinition(
    None,
    None,
    DeviceDocumentation(
        "PicoScope 6404E",
        "oscilloscopes/picoscope-6404e",
        "USB 3.x + ps6000a SDK",
        "PicoSDK 64 位、ps6000a.dll 和 USB 驱动",
    ),
    Path(__file__).parent,
    deployment_components=("pico-sdk",),
    model=DeviceModel(
        "pico.6404e",
        ("Pico Technology",),
        ("6404E",),
        "oscilloscope",
        SOURCE,
        "SDK headers/examples",
        "block/streaming",
    ),
    factory=create_adapter,
    capability_schema=PicoAdapter.capability_schema,
    discovery_enumerator=enumerate_pico,
    discovery_transports=("pico",),
    discovery_connection_only_transports=("pico",),
    supported_transports=("pico",),
    identity_requires_adapter=True,
    acquisition_kinds=("waveform", "streaming"),
    acquisition_initiate_default=True,
    acquisition_initiate_editable=False,
)
