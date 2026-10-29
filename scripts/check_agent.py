"""Verify Agent transport against isolated Mock; never discovers or opens hardware."""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from lab_control.agent.verification import verify_mock
from lab_control.application.runtime import ApplicationRuntime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connector", type=Path, help="Optional built console connector EXE")
    parser.add_argument("--codex", type=Path, help="Optional installed Codex CLI for actual Agent acceptance")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / ".tools/agent-checks" / uuid4().hex
    with ApplicationRuntime(root=root, hardware=False) as runtime:
        result = verify_mock(runtime, connector=args.connector, codex=args.codex)
    path = root / "report.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("Report: " + str(path))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
