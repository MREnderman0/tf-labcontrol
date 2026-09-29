"""53100A / TimeLab TSERVE metadata; importing this module never opens hardware."""

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import Capability, CapabilitySet, Parameter
from lab_control.instruments.base import CommandSpec, Profile

SOURCE = "https://ww1.microchip.com/downloads/aemDocuments/documents/FTD/ProductDocuments/UserGuides/53100A-Phase-Noise-Analyzer-Users-Guide-DS50002991.pdf"
RELEASE_NOTES = "https://www.miles.io/timelab/readme.htm"
FREQUENCIES = {"input_frequency": "inputfreq", "reference_frequency": "referencefreq"}


def profile() -> Profile:
    commands = {}
    commands["sample_rate"] = CommandSpec(
        Capability(
            "sample_rate",
            {"value": Parameter("integer", unit="samples/s", choices=("1", "10", "100", "1000"))},
            readable=True,
            writable=True,
            section="8.3 set/show phaserate",
        ),
        query="show phaserate",
    )
    commands["timed_phase"] = CommandSpec(
        Capability("timed_phase", readable=True, section="8.2 /format:P /timestamp:s /phasedec:2")
    )
    for name in ("start", "stop"):
        commands[name] = CommandSpec(
            Capability(name, writable=True, requires_confirmation=True, section="8.3 " + name), (name,)
        )
    for name, wire in FREQUENCIES.items():
        commands[name] = CommandSpec(
            Capability(
                name,
                {
                    "value": Parameter(unit="Hz", minimum=1e6, maximum=200e6, required=False),
                    "automatic": Parameter("boolean", required=False),
                },
                readable=True,
                writable=True,
                section="8.3 set/show " + wire,
            ),
            query="show " + wire,
        )
    for name, wire in (("phase_noise", "spectrum"), ("am_noise", "amspectrum"), ("stability", "adev")):
        commands[name] = CommandSpec(
            Capability(name, readable=True, section="8.3 show " + wire), query="show " + wire
        )
    commands["data_stream"] = CommandSpec(
        Capability("data_stream", readable=True, section="8.2 /format /timestamp /phaserate /phasedec")
    )
    return Profile(
        "microchip.53100a",
        ("Microchip", "PhaseStation"),
        ("53100A",),
        "phase_noise_analyzer",
        (1,),
        SOURCE,
        "DS50002991G; TimeLab 1.780A release notes",
        commands,
        identity_query="show serial",
        error_query="",
        acquisition="phase_noise/am_noise/stability/streaming/timed_phase",
    )


def capability_schema(config: ConnectionConfig) -> CapabilitySet:
    from dataclasses import replace

    declared = profile()
    caps = []
    for spec in declared.commands.values():
        cap = replace(spec.capability, source=SOURCE)
        if cap.name == "timed_phase":
            cap = replace(
                cap,
                available=bool(config.usb_instance_id),
                reason="Requires managed USB TSERVE with verified timestamped phase format",
            )
        if cap.name == "data_stream":
            cap = replace(
                cap,
                available=bool(config.data_resource and config.stream_format and not config.usb_instance_id),
                reason="Requires explicit data_resource, P/F format, timestamp off and matching stream_interval",
            )
        caps.append(cap)
    return CapabilitySet(declared.category, declared.channels, tuple(caps))
