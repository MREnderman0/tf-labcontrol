from __future__ import annotations

from dataclasses import replace

from lab_control.domain.instruments import Parameter
from lab_control.instruments.base import (
    Profile,
    ScpiAdapter,
    command,
    parameter,
    read_command,
)

BOOL = Parameter("boolean")
# Historical exported reference; the exact IT6322 protocol source remains in it6322().
ITECH_SOURCE = "https://usa.itechate.com/uploadfiles/%E7%94%A8%E6%88%B7%E6%89%8B%E5%86%8C/user%20manual/it6300/IT6300%20Programming%20Guide-EN.pdf"


def it6322() -> Profile:
    source = "https://jdingtech.com.tw/Official/sharefile/ITECH_Protocol_IT6322_V1.0.0.1.pdf"
    select = ("INST:NSEL {ch}",)
    commands = {}
    for name, path, param, confirm in (
        ("voltage", "VOLT", parameter("V", 0, 30), False),
        ("current", "CURR", parameter("A", 0, 3), False),
        ("voltage_limit", "VOLT:PROT", parameter("V", 0, 30), False),
        ("output", "OUTP", BOOL, True),
        ("timer_delay", "OUTP:TIM:DATA", parameter("s", 0, 999999), False),
    ):
        commands[name] = command(name, path, param, select=select, confirm=confirm)
    for name, path in (
        ("measured_voltage", "MEAS:VOLT?"),
        ("measured_current", "MEAS:CURR?"),
        ("measured_power", "MEAS:POW?"),
    ):
        commands[name] = replace(read_command(name, path), select=select)
    return Profile(
        "itech.it6322",
        ("ITECH",),
        ("IT6322",),
        "power_supply",
        (1, 2, 3),
        source,
        "Manufacturer manual IT6322-402205 V2.2 (2018-03-30), distributor-hosted copy, chapters 3-8",
        commands,
        ("voltage", "current", "output"),
        acquisition="power",
        channel_limits={3: {"voltage": 5, "voltage_limit": 5}},
    )


class It6322Adapter(ScpiAdapter):
    """itech.it6322 control and scalar measurements."""

    def acquire_measurements(self, request):
        from lab_control.instruments.common.scalar import acquire_power

        return acquire_power(self, request)
