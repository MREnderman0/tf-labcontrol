"""One native AUToscale transaction; completion never replays the setting."""

from lab_control.domain.instruments import (
    ApplyResult,
    ControlUpdate,
    DeviceError,
    ParameterReading,
    Unsupported,
)
from lab_control.instruments.base import number

from .profile import CHANNEL_SETTINGS, GLOBAL_SETTINGS

AUTO_TIMEOUT = 30.0


def apply_auto(adapter, command):
    _, parameters = adapter.validate_command(command)
    bounded = getattr(adapter.transport, "query_timeout", None)
    if not callable(bounded):
        raise Unsupported("Auto requires a transport with a bounded completion query")
    before, _ = adapter.snapshot()
    details = {"before": before, "state": "pending", "command_attempted": False}
    adapter.capability_details["auto_setup"] = details
    completed = 0
    attempted = False
    try:
        # AUToscale affects all enabled channels and horizontal/trigger settings.
        # Invalidate range evidence even if the write's result becomes uncertain.
        adapter._ranges.clear()
        details["command_attempted"] = attempted = True
        adapter.transport.write("AUT")
        completed = 1
        if number(bounded("*OPC?", AUTO_TIMEOUT)) != 1:
            raise DeviceError("Auto completion was not confirmed", "configuration_uncertain")
        error = adapter.transport.query("SYST:ERR?").decode("ascii").strip()
        if not error.lstrip("+").startswith("0,"):
            details.update(state="rejected", error=error)
            return ApplyResult("rejected", "auto_setup", parameters, completed_steps=completed, error=error)
        # Fresh model-owned ranges and full readings precede UI synchronization.
        # Waveform stability remains a separate full-frame acquisition check.
        adapter.refresh_capabilities()
        adapter.read("trigger_mode")
        keys = [f"1.{name}" for name in (*GLOBAL_SETTINGS, "trigger_mode")]
        keys += [f"{ch}.{name}" for ch in adapter.profile.channels for name in CHANNEL_SETTINGS]
        readings = tuple(
            ParameterReading(
                key.split(".", 1)[1],
                int(key.split(".", 1)[0]),
                adapter._settings[key],
                "instrument",
                adapter._stamps[key],
            )
            for key in keys
        )
        details.update(state="passed", after={key: adapter._settings[key] for key in keys})
        return ApplyResult(
            "success",
            "auto_setup",
            parameters,
            completed_steps=completed,
            control_update=ControlUpdate(readings, tuple((r.channel, r.operation) for r in readings), True),
        )
    except Exception as exc:
        details.update(state="uncertain", error=str(exc))
        adapter.capability_error = "Auto 后状态未确认，未重发命令：" + str(exc)
        adapter._ranges.clear()
        return ApplyResult(
            "partial" if completed else "uncertain",
            "auto_setup",
            parameters,
            completed_steps=completed,
            error=adapter.capability_error if attempted else str(exc),
        )
