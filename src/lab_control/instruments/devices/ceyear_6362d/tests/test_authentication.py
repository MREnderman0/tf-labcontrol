import json

import pytest

from lab_control.domain.instruments import DeviceError
from lab_control.instruments.devices.ceyear_6362d import authentication as auth
from lab_control.instruments.devices.ceyear_6362d.hardware.validate_control import AuditTransport
from lab_control.instruments.devices.ceyear_6362d.profile import profile
from lab_control.transport.diagnostics import capture_io


class LoginTransport:
    resource = "mock://login"

    def __init__(self, challenge=b"AUTHENTICATE CRAM-MD5.", reply=b"ready"):
        self.challenge, self.reply, self.history = challenge, reply, []

    def query(self, command):
        self.history.append(command)
        return self.challenge

    def write(self, command):
        self.history.append(command)

    def read(self):
        return self.reply


@pytest.mark.parametrize("challenge", [b"AUTHENTICATE CRAM-MD5", b"AUTHENTICATE CRAM-MD5.\r\n"])
def test_documented_login_and_observed_punctuation(challenge):
    transport = LoginTransport(challenge)
    auth.authenticate(transport, ("anonymous", "test-only"))
    assert transport.history == ['open "anonymous"', b"test-only\n"]


def test_wrong_challenge_does_not_send_password():
    transport = LoginTransport(b"unknown challenge")
    with pytest.raises(DeviceError):
        auth.authenticate(transport, ("anonymous", "test-only"))
    assert len(transport.history) == 1


def test_rejected_password_is_not_retried_or_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_CONTROL_IO_DIAGNOSTICS", "1")
    transport = LoginTransport(reply=b"false")
    wire = AuditTransport(transport, tmp_path, profile())
    with capture_io() as capture, pytest.raises(DeviceError, match="authentication rejected"):
        wire.authenticate(("anonymous", "SYNTHETIC-SECRET"), "\n")
    assert transport.history.count(b"SYNTHETIC-SECRET\n") == 1
    assert "SYNTHETIC-SECRET" not in (tmp_path / "communication.jsonl").read_text()
    assert "SYNTHETIC-SECRET" not in json.dumps(capture.details)


@pytest.mark.parametrize("secret", ["", "test-only-secret"])
def test_credentials_roundtrip_are_local_and_scoped(tmp_path, monkeypatch, secret):
    original = auth.credential_path
    monkeypatch.setattr(auth, "credential_path", lambda resource: tmp_path / original(resource).name)
    resource = "TCPIP0::example.invalid::10001::SOCKET"
    path = auth.save_credentials(resource, "anonymous", secret)
    assert auth.load_credentials(resource) == ("anonymous", secret)
    assert auth.load_credentials(resource.lower()) == ("anonymous", secret)
    assert json.loads(path.read_text())["password"] == secret
    with pytest.raises(DeviceError, match="not configured"):
        auth.load_credentials("TCPIP0::another.invalid::10001::SOCKET")
