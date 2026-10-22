"""3458A VISA text records use the model's line-framing contract."""

from lab_control.domain.connections import ConnectionConfig
from lab_control.transport.base import Transport
from lab_control.transport.visa import VisaRuntime, VisaTransport


def create_transport(config: ConnectionConfig, runtime: VisaRuntime) -> Transport:
    if config.transport == "visa":
        return VisaTransport(
            runtime,
            config.resource,
            config.timeout,
            config.terminator,
            config.baud_rate,
            line_framing=True,
        )
    from lab_control.instruments.registry import default_transport_for

    return default_transport_for(config, runtime)
