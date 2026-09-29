"""Model-owned B.11 Socket authentication and local plaintext configuration."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
from pathlib import Path

from lab_control.application.config import ConnectionConfig, canonical_resource
from lab_control.domain.instruments import DeviceError, DeviceIdentity
from lab_control.instruments.base import identity_from_scpi
from lab_control.storage.repository import application_home


def credential_path(resource: str) -> Path:
    name = hashlib.sha256(canonical_resource(resource).encode()).hexdigest()
    project = Path(__file__).resolve().parents[5]
    root = project / ".local" if (project / "pyproject.toml").is_file() else application_home() / "config"
    return root / "device-auth" / "ceyear_6362d" / (name + ".json")


def validate(username: str, password: str) -> None:
    if not username or len(username) > 128 or len(password) > 256:
        raise DeviceError("Invalid remote credential length", "authentication_required")
    if any(c in username for c in '\r\n;"') or any(c in password for c in "\r\n"):
        raise DeviceError("Remote credentials must be a single protocol line", "authentication_required")
    try:
        username.encode("ascii")
        password.encode("ascii")
    except UnicodeError as exc:
        raise DeviceError("6362D remote credentials must be ASCII", "authentication_required") from exc


def save_credentials(resource: str, username: str, password: str) -> Path:
    validate(username, password)
    path = credential_path(resource)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"version": 1, "username": username, "password": password}), encoding="utf-8"
    )
    temporary.replace(path)
    return path


def load_credentials(resource: str) -> tuple[str, str]:
    path = credential_path(resource)
    if not path.exists():
        raise DeviceError(
            "6362D remote credentials are not configured; use the model authentication entry",
            "authentication_required",
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (
            data["version"] != 1
            or not isinstance(data["username"], str)
            or not isinstance(data["password"], str)
        ):
            raise ValueError("schema")
        username, password = data["username"], data["password"]
        validate(username, password)
        return username, password
    except (KeyError, ValueError, TypeError) as exc:
        raise DeviceError("Invalid 6362D credential file", "authentication_required") from exc


def authenticate(transport, credentials: tuple[str, str], terminator: str = "\n") -> None:
    username, password = credentials
    validate(username, password)
    reply = transport.query(f'open "{username}"').decode("ascii").strip()
    if reply not in {"AUTHENTICATE CRAM-MD5", "AUTHENTICATE CRAM-MD5."}:
        raise DeviceError(
            f"Unsupported 6362D authentication challenge: {reply[:160]!r}", "authentication_failed"
        )
    # Raw write avoids diagnostic query mnemonic capture of a password prefix.
    transport.write(password.encode("ascii") + terminator.encode("ascii"))
    if transport.read().decode("ascii").strip().lower() != "ready":
        raise DeviceError("6362D remote authentication rejected", "authentication_failed")


def prepare_identity(transport, config: ConnectionConfig) -> None:
    if config.transport in {"visa", "tcp"} and (
        config.resource.upper().endswith("::SOCKET") or config.resource.startswith("tcp://")
    ):
        credentials = load_credentials(config.resource)
        exchange = getattr(transport, "authenticate", None)
        if exchange:
            exchange(credentials, config.terminator)
        else:
            authenticate(transport, credentials, config.terminator)


def discovery_identity(transport, config: ConnectionConfig) -> DeviceIdentity:
    """Only authenticate and read fresh identity; never inherit an old asset's identity."""
    prepare_identity(transport, config)
    return identity_from_scpi(transport.query("*IDN?"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource", required=True)
    parser.add_argument("--username", default="anonymous")
    args = parser.parse_args()
    password = getpass.getpass("6362D remote password: ")
    path = save_credentials(args.resource, args.username, password)
    print(f"Local plaintext remote configuration saved: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
