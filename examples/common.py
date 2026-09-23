from __future__ import annotations

import argparse
import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from lab_control.application.config import load_config
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceCommand, DeviceError, jsonable


def parser(description: str) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=description)
    result.add_argument("--config", type=Path, required=True)
    result.add_argument("--resource", required=True)
    result.add_argument("--mock", action="store_true")
    result.add_argument("--write", action="store_true")
    result.add_argument("--confirm", action="store_true")
    result.add_argument("--channel", type=int, default=1)
    result.add_argument("--capture", action="store_true")
    return result


@contextmanager
def connected(args):
    configs = [config for config in load_config(args.config) if config.resource == args.resource]
    if len(configs) != 1:
        raise DeviceError("Select exactly one configured resource")
    config = replace(configs[0], transport="mock") if args.mock else configs[0]
    with ApplicationRuntime((config,), hardware=not args.mock) as app:
        device_id = app.add(config)
        identity = app.connect(device_id).result()
        print(
            json.dumps(
                jsonable(
                    {
                        "identity": identity,
                        "resource": config.resource,
                        "capabilities": app.sessions.submit(device_id, "capabilities").result(),
                        "status": app.sessions.submit(device_id, "status").result(),
                    }
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        yield app, device_id


def apply(app, device_id, args, operation, parameters):
    if not args.write:
        raise DeviceError("Add --write to authorize these settings", "confirmation_required")
    print(
        json.dumps(
            {"target": args.resource, "operation": operation, "parameters": parameters}, ensure_ascii=False
        )
    )
    result = app.sessions.submit(
        device_id, "apply", DeviceCommand(operation, parameters, args.channel, args.confirm)
    ).result()
    if result.outcome != "success":
        raise DeviceError(f"{result.outcome}: {result.error}; do not automatically repeat")
    return result
