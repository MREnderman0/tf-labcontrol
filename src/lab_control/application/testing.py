"""Explicit hardware acceptance helpers, also runnable with the isolated simulation runtime."""

from typing import Any

from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest, ValidationError, utc_now


def scan_stress(
    app: ApplicationRuntime,
    resources: list[str],
    repeats: int = 3,
    capture_resource: str = "",
    request: AcquisitionRequest | None = None,
) -> dict[str, Any]:
    if not resources or len(set(resources)) != len(resources) or not 1 <= repeats <= 100:
        raise ValidationError("Choose unique resources and 1..100 scan repetitions")
    selected = [config for config in app.configurations if config.resource in resources]
    if len(selected) != len(resources) or (capture_resource and capture_resource not in resources):
        raise ValidationError("Every selected/capture resource must match one configuration")
    report: dict[str, Any] = {
        "timestamp": utc_now(),
        "connections": {},
        "scans": [],
        "hardware_accepted": False,
    }
    ids = {}
    for config in selected:
        device_id = app.add(config)
        ids[config.resource] = device_id
        try:
            identity = app.connect(device_id).result()
            report["connections"][config.resource] = {"identity": identity, "state": "connected"}
        except Exception as exc:
            report["connections"][config.resource] = {"state": "failed", "error": str(exc)}
    report["before"] = app.sessions.snapshots()
    stream = None
    capture_id = ids.get(capture_resource, "")
    try:
        if capture_id:
            if request is None:
                raise ValidationError("Supply a capture request")
            stream = app.sessions.start_acquisition(capture_id, request, interval=0.01)
        for _ in range(repeats):
            report["scans"].append(app.discovery.scan().result())
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        if stream:
            app.sessions.stop_acquisition(capture_id)
            try:
                report["capture_paths"] = stream.result()
            except Exception as exc:
                report["capture_error"] = str(exc)
    report["after"] = app.sessions.snapshots()
    report["sessions_unchanged"] = {s["stableDeviceId"]: s["sessionId"] for s in report["before"]} == {
        s["stableDeviceId"]: s["sessionId"] for s in report["after"]
    }
    report["software_checks_passed"] = (
        report["sessions_unchanged"]
        and not report.get("error")
        and not report.get("capture_error")
        and (not capture_resource or bool(report.get("capture_paths")))
        and len(report["scans"]) == repeats
        and all(scan["state"] == "complete" for scan in report["scans"])
        and all(c["state"] == "connected" for c in report["connections"].values())
    )
    return report
