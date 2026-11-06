"""Explicit query-only verification of a selected SPS5000X asset."""

import argparse
import json
from dataclasses import replace
from datetime import datetime

from lab_control.domain.connections import ConnectionConfig
from lab_control.instruments.common.readonly_hardware import asset_folder_name, verify
from lab_control.storage.hardware_documents import refresh_after_batch
from lab_control.storage.repository import application_home

from .. import DEFINITION
from ..adapter import ADAPTER_ID, RATINGS, rated_profile
from .records import publish_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=tuple(RATINGS), required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--resource", required=True)
    parser.add_argument("--transport", choices=DEFINITION.supported_transports, default="visa")
    args = parser.parse_args()
    serial = args.serial.strip()
    if not serial or serial.upper() in {"0", "N/A", "UNKNOWN", "NONE", "-"}:
        parser.error("Require a full instrument serial number")
    config = ConnectionConfig(args.resource, ADAPTER_ID, transport=args.transport, expected_serial=serial)
    assert DEFINITION.profile is not None
    # The common query whitelist must include every channel of the selected submodel.
    definition = replace(DEFINITION, profile=rated_profile(DEFINITION.profile, args.model))
    folder = (
        application_home()
        / "hardware-tests"
        / asset_folder_name(args.model, serial)
        / (datetime.now().strftime("%Y-%m-%d-%H%M%S-%f") + "-sps5000x-readonly")
    )
    folder.mkdir(parents=True, exist_ok=False)
    result = verify(definition, config, folder, expected_model=args.model)
    publish_report(folder)
    refresh_after_batch(folder)
    print(folder, flush=True)
    print(json.dumps(result["stages"], ensure_ascii=False), flush=True)
    return int(bool(result.get("error") or result.get("close_error")))


if __name__ == "__main__":
    raise SystemExit(main())
