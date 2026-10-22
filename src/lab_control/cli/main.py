from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from lab_control.application.config import ConnectionConfig, load_config
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError, jsonable, utc_now
from lab_control.domain.workspace import GENERAL, WORKSPACE_MODES
from lab_control.instruments.registry import PENDING, PROFILES, SDK_ADAPTERS


def output(value: Any) -> None:
    print(json.dumps(jsonable(value), ensure_ascii=False, indent=2, allow_nan=False))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="TF LabControl hardware test tools. Default operations are read-only."
    )
    root.add_argument("--config", type=Path, help="UTF-8 TOML configuration")
    root.add_argument("--resource", help="Exact resource; always select explicitly for hardware tests")
    root.add_argument("--adapter", help="Exact registered model adapter ID")
    root.add_argument("--transport", default="visa")
    root.add_argument("--home", type=Path)
    root.add_argument(
        "--mock", action="store_true", help="Isolated simulation; never opens physical transports"
    )
    sub = root.add_subparsers(dest="action", required=True)
    analysis = sub.add_parser("export-analysis", help="Offline Excel/Origin export; no instrument access")
    analysis.add_argument("source", type=Path, help="Existing recording or snapshot directory")
    analysis.add_argument("--output", type=Path, required=True, help="Separate new output directory")
    environment = sub.add_parser("environment", help="Inspect environment without instrument access")
    environment.add_argument("--report", type=Path)
    environment.add_argument("--repair-plan", action="store_true")
    components = sub.add_parser("components", help="Explicit component download/verification; never install")
    components.add_argument("component_action", choices=("list", "prepare", "verify"))
    components.add_argument("--component", action="append", default=[])
    components.add_argument("--cache", type=Path)
    components.add_argument("--offline", action="store_true")
    components.add_argument("--report", type=Path)
    network = sub.add_parser("network", help="Read-only per-computer Ethernet/DHCP deployment planning")
    network.add_argument("network_action", choices=("inspect", "plan", "validate"))
    network.add_argument("--interface", default="")
    network.add_argument("--server-address", default="")
    network.add_argument("--pool-start", default="")
    network.add_argument("--pool-size", type=int, default=100)
    network.add_argument("--lease-minutes", type=int, default=1440)
    network.add_argument("--exclude", action="append", default=[])
    network.add_argument("--plan-file", type=Path, help="New plan destination or existing plan to validate")
    for name in (
        "doctor",
        "scan",
        "models",
        "identity",
        "capabilities",
        "refresh-capabilities",
        "status",
        "test",
    ):
        sub.add_parser(name)
    stress = sub.add_parser("scan-stress", help="Explicit multi-device scan/session preservation report")
    stress.add_argument("--resources", nargs="+", required=True)
    stress.add_argument("--repeats", type=int, default=3)
    stress.add_argument("--capture-resource", default="")
    stress.add_argument("--kind", default="waveform")
    stress.add_argument("--points", type=int, default=1000)
    stress.add_argument("--initiate", action="store_true")
    stress.add_argument("--confirm", action="store_true")
    readonly = sub.add_parser(
        "verify-readonly", help="Run the selected model's documented read-only acceptance"
    )
    readonly.add_argument("--folder", type=Path, required=True)
    readonly.add_argument("--duration", type=float, default=60)
    readonly.add_argument("--input-condition", default="未核对")
    apply = sub.add_parser("set")
    apply.add_argument("operation")
    apply.add_argument("--parameters", type=Path, required=True, help="UTF-8 JSON file of typed parameters")
    apply.add_argument("--channel", type=int, default=1)
    apply.add_argument("--write", action="store_true", help="Authorize the displayed configuration change")
    apply.add_argument("--confirm", action="store_true", help="Additional confirmation for outputs/triggers")
    for name in ("acquire", "stream"):
        capture = sub.add_parser(name)
        capture.add_argument("--kind", default="scalar")
        capture.add_argument("--quantity", default="")
        capture.add_argument("--channels", default="1")
        capture.add_argument("--points", type=int, default=1000)
        capture.add_argument("--initiate", action="store_true")
        capture.add_argument("--confirm", action="store_true")
        capture.add_argument("--mode", choices=WORKSPACE_MODES, default=GENERAL)
        if name == "stream":
            capture.add_argument("--count", type=int, default=10)
            capture.add_argument("--interval", type=float, default=0.5)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.action == "models":
        output(
            {
                "models": {
                    key: {"models": p.models, "category": p.category, "operations": tuple(p.commands)}
                    for key, p in PROFILES.items()
                },
                "sdk": SDK_ADAPTERS,
                "pending": PENDING,
            }
        )
        return 0
    app: ApplicationRuntime | None = None
    try:
        if args.action == "components":
            from lab_control.deployment.environment import export_report
            from lab_control.deployment.materials import components_cli

            report = components_cli(args)
            if args.report:
                export_report(report, args.report)
            output(report)
            return 0 if report.get("passed", True) else 2
        if args.action == "export-analysis":
            from lab_control.storage.analysis import export_analysis

            output({"folder": str(export_analysis(args.source, args.output))})
            return 0
        if args.action == "environment":
            from lab_control.deployment.environment import export_report, inspect_environment, plan_repair
            from lab_control.discovery.lan import load_lan_settings
            from lab_control.storage.repository import application_home

            home = args.home or application_home()
            configs = (
                load_config(args.config)
                if args.config
                else (
                    load_config(home / "config/devices.toml")
                    if (home / "config/devices.toml").is_file()
                    else ()
                )
            )
            report = inspect_environment(home, configs, lan=load_lan_settings(home).enabled)
            if args.report:
                export_report(report, args.report)
            output(plan_repair(report) if args.repair_plan else report)
            return 0
        if args.action == "network":
            from lab_control.application.network_setup import inspect_network, plan_dhcp, validate_plan

            snapshot = inspect_network()
            if args.network_action == "inspect":
                output(snapshot)
            elif args.network_action == "validate":
                if not args.plan_file:
                    raise ValueError("Specify --plan-file for validation")
                plan = json.loads(args.plan_file.read_text(encoding="utf-8-sig"))
                validate_plan(plan, snapshot)
                output({"plan_matches_this_computer": True, "network_changed": False})
            else:
                plan = plan_dhcp(
                    snapshot,
                    args.interface,
                    server_address=args.server_address,
                    pool_start=args.pool_start,
                    pool_size=args.pool_size,
                    lease_minutes=args.lease_minutes,
                    exclusions=tuple(args.exclude),
                )
                if args.plan_file:
                    with args.plan_file.open("x", encoding="utf-8") as stream:
                        json.dump(plan, stream, ensure_ascii=False, indent=2)
                output(plan)
            return 0
        configs = load_config(args.config) if args.config else ()
        if args.resource and args.adapter:
            if not any(config.resource == args.resource for config in configs):
                configs += (
                    ConnectionConfig(args.resource, args.adapter, "mock" if args.mock else args.transport),
                )
        if args.mock:
            from dataclasses import replace

            configs = tuple(replace(config, transport="mock") for config in configs)
        if args.action == "verify-readonly":
            from lab_control.instruments.registry import DEVICE_DEFINITIONS

            if args.mock or not args.resource or not args.adapter:
                raise ValueError("Specify one explicit real --resource and --adapter")
            selected = [c for c in configs if c.resource == args.resource and c.adapter == args.adapter]
            definition = DEVICE_DEFINITIONS.get(args.adapter)
            if len(selected) != 1 or definition is None or definition.readonly_check is None:
                raise ValueError("The selected model has no registered read-only acceptance entry")
            if not 1 <= args.duration <= 3600:
                raise ValueError("Read-only duration must be 1..3600 seconds")
            report = definition.readonly_check(
                selected[0],
                args.folder,
                duration=args.duration,
                root=args.home,
                input_condition=args.input_condition,
            )
            output(report)
            return 0 if report.get("checks_passed") else 1
        with ApplicationRuntime(configs, root=args.home, hardware=not args.mock) as app:
            if args.action == "doctor":
                output(app.diagnostics())
                return 0
            if args.action == "scan":
                output({"scan": app.discovery.scan().result(), "devices": app.inventory.all()})
                return 0
            if args.action == "scan-stress":
                from lab_control.application.testing import scan_stress

                report = scan_stress(
                    app,
                    args.resources,
                    args.repeats,
                    args.capture_resource,
                    AcquisitionRequest(
                        args.kind, points=args.points, initiate=args.initiate, confirmed=args.confirm
                    ),
                )
                output(
                    {
                        "report": str(app.repository.report(report)),
                        "checks_passed": report["software_checks_passed"],
                    }
                )
                return 0 if report["software_checks_passed"] else 2
            if not args.resource:
                raise DeviceError(
                    "Specify --resource explicitly; a single configured device is not auto-selected",
                    "device_required",
                )
            selected = [config for config in configs if config.resource == args.resource]
            if len(selected) != 1:
                raise DeviceError(
                    "Select one unique resource from --config or supply --adapter", "device_required"
                )
            device_id = app.add(selected[0])
            identity = app.connect(device_id).result()
            sessions = app.sessions
            if args.action == "identity":
                output(identity)
            elif args.action in {"capabilities", "status"}:
                output(sessions.submit(device_id, args.action).result())
            elif args.action == "refresh-capabilities":
                output(sessions.submit(device_id, "refresh_capabilities").result())
            elif args.action == "set":
                parameters = json.loads(args.parameters.read_text(encoding="utf-8-sig"))
                output(
                    {
                        "target": identity,
                        "resource": args.resource,
                        "operation": args.operation,
                        "channel": args.channel,
                        "parameters": parameters,
                    }
                )
                if not args.write:
                    raise DeviceError(
                        "Preview only: add --write to authorize this change", "confirmation_required"
                    )
                result = sessions.submit(
                    device_id, "apply", DeviceCommand(args.operation, parameters, args.channel, args.confirm)
                ).result()
                output(
                    {
                        "result": result,
                        "report": str(
                            app.repository.report(
                                {
                                    "timestamp": utc_now(),
                                    "resource": args.resource,
                                    "identity": identity,
                                    "command": args.operation,
                                    "parameters": parameters,
                                    "result": result,
                                    "hardware_accepted": False,
                                }
                            )
                        ),
                    }
                )
                if result.outcome != "success":
                    return 2
            elif args.action in {"acquire", "stream"}:
                request = AcquisitionRequest(
                    args.kind,
                    tuple(int(c) for c in args.channels.split(",")),
                    args.points,
                    args.quantity,
                    args.initiate,
                    args.confirm,
                )
                if args.action == "stream":
                    paths = sessions.start_acquisition(
                        device_id, request, interval=args.interval, count=args.count, mode=args.mode
                    ).result()
                    output(
                        {
                            "paths": paths,
                            "report": str(
                                app.repository.report(
                                    {
                                        "resource": args.resource,
                                        "identity": identity,
                                        "request": request,
                                        "paths": paths,
                                        "hardware_accepted": False,
                                    }
                                )
                            ),
                        }
                    )
                else:
                    context = app.repository.new_save_context(
                        args.mode, "single_frame", device_id=device_id, identity=identity
                    )
                    batch = sessions.submit(device_id, "acquire", request).result()
                    path = app.repository.export(batch, args.mode, context=context)
                    output(
                        {
                            "path": str(path),
                            "timestamp": batch.timestamp,
                            "quality": [measurement.quality for measurement in batch.measurements],
                            "report": str(
                                app.repository.report(
                                    {
                                        "identity": identity,
                                        "resource": args.resource,
                                        "request": request,
                                        "data_path": str(path),
                                        "quality": [m.quality for m in batch.measurements],
                                        "hardware_accepted": False,
                                    }
                                )
                            ),
                        }
                    )
                    if any(
                        measurement.quality not in {"good", "simulated"} for measurement in batch.measurements
                    ):
                        return 2
            else:
                report = {
                    "timestamp": utc_now(),
                    "identity": identity,
                    "resource": args.resource,
                    "capabilities": sessions.submit(device_id, "capabilities").result(),
                    "capability_snapshot": sessions.capability_snapshot(device_id),
                    "status": sessions.submit(device_id, "status").result(),
                    "test_scope": "identity/capabilities/status only; no writes or output changes",
                    "hardware_accepted": False,
                }
                output({"report": str(app.repository.report(report))})
        return 0
    except (DeviceError, ValueError, OSError, KeyError) as exc:
        failure = {
            "error": str(exc),
            "code": getattr(exc, "code", "invalid_input"),
            "resource": args.resource,
            "action": args.action,
            "timestamp": utc_now(),
            "hardware_accepted": False,
        }
        if app is not None and args.action in {"test", "set", "acquire", "stream"}:
            failure["report"] = str(app.repository.report(failure))
        output(failure)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
