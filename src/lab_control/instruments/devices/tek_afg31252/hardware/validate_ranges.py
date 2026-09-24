"""AFG31252 coupled-limit verification plus the reversible USB control regression.

Invalid candidates only enter validate_command/preflight on the session worker;
they never enter apply/encode/write. Legal settings use the ordinary session API.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from lab_control.domain.instruments import DeviceCommand, Unsupported, ValidationError, utc_now

from .validate_usb import Acceptance, main


class RangeAcceptance(Acceptance):
    def basic(self) -> None:
        super().basic()
        self.group("range_controls", self.range_controls)

    def rejected(self, channel: int, operation: str, value) -> None:
        self.outputs_off()
        count = sum(g.write_count for g in self.guards)
        command = DeviceCommand(operation, {"value": value}, channel, True)
        entry = {
            "time": utc_now(),
            "channel": channel,
            "operation": operation,
            "parameters": command.parameters,
            "validation_only": True,
        }
        self.report.setdefault("range_rejections", []).append(entry)

        def check(adapter):
            _, values = adapter.validate_command(command)
            adapter.preflight(command, values)

        try:
            self.worker(check)
        except (ValidationError, Unsupported) as exc:
            entry.update(status="passed", reason=str(exc))
        else:
            entry.update(status="failed", reason="Invalid candidate unexpectedly accepted")
            raise RuntimeError(entry["reason"])
        finally:
            entry["setting_writes"] = sum(g.write_count for g in self.guards) - count
            self.save()
        if entry["setting_writes"]:
            raise RuntimeError("Validation-only case produced a setting write")
        self.outputs_off()

    def range_controls(self) -> None:
        source = Path(__file__).read_bytes()
        (self.folder / "range_entry.py").write_bytes(source)
        self.report["range_entry_sha256"] = hashlib.sha256(source).hexdigest()
        for ch in (1, 2):
            self.quiet(ch)
            self.apply(ch, "offset", 0)
            self.apply(ch, "amplitude", 0.1)
            self.apply(ch, "waveform", "SIN")
            for frequency in (1e3, 200e6, 210e6, 250e6):
                self.apply(ch, "frequency", frequency)
            self.rejected(ch, "frequency", 250e6 + 1)
            # This threshold applies only to the observed 50-ohm load; never change the load.
            if float(self.baseline[f"OUTP{ch}:IMP?"]) == 50:
                self.rejected(ch, "amplitude", 4.001)
                self.apply(ch, "frequency", 1000)
                self.apply(ch, "amplitude", 0.001)
                self.apply(ch, "amplitude", 0.2)
                self.rejected(ch, "offset", 2.401)
            for waveform, maximum in (("SQU", 160e6), ("RAMP", 2.5e6)):
                self.apply(ch, "frequency", 1000)
                self.apply(ch, "waveform", waveform)
                self.apply(ch, "frequency", maximum)
                self.rejected(ch, "frequency", maximum + 1)
            self.rejected(ch, "sweep_stop", 2.5e6 + 1)
            self.apply(ch, "frequency", 1000)
            self.apply(ch, "waveform", "PULS")
            self.rejected(ch, "pulse_width", 0.001)
            self.rejected(ch, "pulse_width", 3e-9)
            self.apply(ch, "pulse_width", 0.00025)
            self.apply(ch, "waveform", "SIN")


if __name__ == "__main__":
    main(RangeAcceptance, "usb-range-control")
