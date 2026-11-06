"""Bounded acceptance of software temperatures through the formal recorder."""

import time
from pathlib import Path

from lab_control.domain.instruments import AcquisitionRequest, DeviceError, jsonable
from lab_control.storage.recording import read_frames

from ..connection_setup import validate_channels
from ..protocol import scan_channels, token
from ..thermistor_curves import temperature
from .validate_control import save_json


def parse_curve_groups(groups):
    channels = {}
    for group in groups:
        selection, curve = group.split("=")
        for ch in scan_channels(f"(@{selection})"):
            if ch in channels:
                raise ValueError(f"Duplicate channel {ch}")
            channels[ch] = curve
    validate_channels(channels, "C")
    return dict(sorted(channels.items()))


class SoftwareTemperatureTest:
    def __init__(self, app, device, folder, report, curves):
        self.app, self.device, self.folder, self.report = app, device, folder, report
        self.channels = tuple(sorted(curves))

    def q(self, query):
        session = self.app.sessions.sessions[self.device]
        return token(session.worker.submit(lambda: session.adapter.transport.query(query)).result())

    def capture(self, label, count=1):
        for index in range(count):
            frame = self.app.sessions.submit(
                self.device, "acquire", AcquisitionRequest("scalar", self.channels, confirmed=True)
            ).result()
            self.validate(jsonable(frame)["measurements"])
            raw = jsonable(frame)
            save_json(self.folder / f"{label}-{index + 1:02}.json", raw)
            self.report["samples"].append({"label": label, "frame": raw})

    def validate(self, measurements):
        if tuple(m["channel"] for m in measurements) != self.channels:
            raise DeviceError("Temperature channel mismatch")
        for m in measurements:
            conversion = m["metadata"]["temperature_conversion"]
            expected = temperature(conversion["resistance_ohm"], conversion["curve_id"])
            if m["quantity"] != "temperature" or m["unit"] != "°C" or abs(m["values"][0] - expected) > 1e-9:
                raise DeviceError("Saved resistance/temperature mismatch")

    def monitor(self, duration):
        app, device = self.app, self.device
        app.monitoring.start(
            device,
            (AcquisitionRequest("scalar", self.channels, confirmed=True),),
            interval=0.5,
            record_interval=None,
            recording=True,
        )
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            state = app.monitoring.snapshots()[device]
            if state.get("error"):
                raise DeviceError(state["error"], "acquisition_failed")
            time.sleep(0.2)
        app.monitoring.stop(device).result(30)
        recorder = app.monitoring.tasks[device].recorder
        if recorder is None:
            raise DeviceError("No recorder was started")
        recorder.finished.result(30)
        state = recorder.snapshot()
        frames = [f for p in recorder.segments.paths for f in read_frames(Path(p))]
        save_json(self.folder / "recorded-frames.json", frames)
        if (
            state["error"]
            or state["csv_error"]
            or not frames
            or not (len(frames) == state["received"] == state["committed"] == state["converted"])
        ):
            raise DeviceError("Recording completeness check failed: " + str(state))
        for frame in frames:
            self.validate(frame["measurements"])
        self.report["temperature_window"] = jsonable(app.monitoring.temperature_window(device))
        self.report["stages"]["monitoring"] = dict(state="passed", frames=len(frames), duration=duration)
        self.report["stages"]["recording"] = dict(
            state="passed", **{k: v for k, v in state.items() if k != "state"}
        )
        unlocked = not app.sessions.sessions[device].controls_locked
        self.report["stages"]["stop"] = dict(
            state="passed" if unlocked else "failed", controls_unlocked=unlocked
        )
        if not unlocked:
            raise DeviceError("Parameters remain locked after recorder stopped", "task_busy")
