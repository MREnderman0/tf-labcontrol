import asyncio
import json
import socket
import sys
import threading
import time

import httpx2
import pytest

from lab_control.agent.connector import remote_client
from lab_control.agent.server import AgentServer
from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceError


def wait_task(service, client, grant, task, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = service.get_task_status(client, grant, task["id"])
        if value["state"] not in {"accepted", "running", "stopping", "converting", "binary_saved"}:
            return value
        time.sleep(0.01)
    raise AssertionError(value)


@pytest.fixture
def experiment(tmp_path):
    config = ConnectionConfig("mock://agent/scope", "rs.rtm3004", "mock")
    with ApplicationRuntime((config,), root=tmp_path, hardware=False) as runtime:
        device = runtime.add(config)
        runtime.connect(device).result()
        service = runtime.agent
        client, token = service.policy.create_client("test client")
        grant = service.policy.create_grant(
            client,
            [device],
            [
                "view",
                "connect",
                "disconnect",
                "read_parameters",
                "apply",
                "acquire",
                "monitor",
                "record",
                "stop",
                "results",
            ],
            channels=[1],
            operations=["timebase"],
            limits={"timebase": {"value": [0.0001, 0.01]}},
        )
        yield runtime, service, client, token, grant, device


def test_agent_changes_share_session_deduplicate_and_reject_stale_preview(experiment):
    runtime, service, client, token, grant, device = experiment
    assert service.policy.authenticate(token) == client
    session = runtime.sessions.sessions[device]
    preview = service.preview_change(client, grant, device, "timebase", {"value": 0.002})
    task = service.apply_change(client, grant, device, preview["id"], "write-1")
    result = wait_task(service, client, grant, task)
    assert result["state"] == "completed", result
    version = session.configuration_version
    assert service.apply_change(client, grant, device, preview["id"], "write-1")["id"] == task["id"]
    assert session.configuration_version == version
    stale = service.apply_change(client, grant, device, preview["id"], "write-2")
    assert wait_task(service, client, grant, stale)["code"] == "configuration_conflict"
    assert session.state == "connected"
    with pytest.raises(DeviceError, match="范围"):
        service.preview_change(client, grant, device, "timebase", {"value": 0.02})


def test_guard_rechecks_grant_inside_device_worker(experiment):
    runtime, service, client, _token, grant, device = experiment
    session = runtime.sessions.sessions[device]
    release = threading.Event()
    blocker = session.worker.submit(release.wait, 3)
    preview = service.preview_change(client, grant, device, "timebase", {"value": 0.002})
    task = service.apply_change(client, grant, device, preview["id"], "revoked-write")
    service.policy.revoke_grant(grant)
    release.set()
    blocker.result()
    deadline = time.monotonic() + 4
    while service.ledger.get(task["id"])["state"] in {"accepted", "running", "stopping"}:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert session.configuration_version == 0 and session.pending == 0
    assert service.ledger.get(task["id"])["code"] == "grant_expired"


def test_shared_monitor_recording_stop_and_artifacts(experiment):
    runtime, service, client, _token, grant, device = experiment
    task = service.start_recording(
        client, grant, device, {"kind": "waveform", "points": 32}, "record-1", 0.05
    )
    deadline = time.monotonic() + 5
    while not runtime.monitoring.latest(device, "waveform"):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    conflict = service.start_monitoring(client, grant, device, {"kind": "waveform"}, "conflict")
    assert wait_task(service, client, grant, conflict)["code"] == "task_conflict"
    latest = service.get_latest_measurement(client, grant, device, "waveform")
    assert latest["available"] and latest["measurements"][0]["points"] == 32
    stop = service.stop_task(client, grant, task["id"], "stop-1")
    assert wait_task(service, client, grant, stop)["state"] == "completed"
    assert wait_task(service, client, grant, task)["state"] == "completed"
    artifacts = service.list_artifacts(client, grant, task["id"])
    assert any(a["name"].endswith(".csv") for a in artifacts)
    assert all("path" not in a for a in artifacts)
    assert not runtime.sessions.sessions[device].controls_locked


def test_sdk_http_auth_origin_and_full_mock_flow(experiment):
    runtime, service, client, token, grant, device = experiment
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    service.server = AgentServer(service)
    service.server.configure({"enabled": True, "local_port": port})
    url = f"http://127.0.0.1:{port}/mcp"
    with httpx2.Client(trust_env=False) as http:
        assert http.post(url, json={}).status_code == 401
        assert (
            http.post(
                url, json={}, headers={"Authorization": "Bearer " + token, "Origin": "http://evil.test"}
            ).status_code
            == 403
        )

    async def run():
        async with remote_client({"url": url}, True, token) as remote:
            listed = await remote.list_tools()
            assert len(listed.tools) == 18
            info = await remote.call_tool("get_server_info", {})
            assert not info.is_error, info
            params = {"grant_id": grant, "device_id": device}
            result = await remote.call_tool(
                "preview_change", {**params, "operation": "timebase", "parameters": {"value": 0.003}}
            )
            assert not result.is_error, result
            data = json.loads(result.content[0].text)["data"]
            result = await remote.call_tool(
                "apply_change", {**params, "preview_id": data["id"], "request_id": "sdk-1"}
            )
            assert not result.is_error, result
            task = json.loads(result.content[0].text)["data"]
            done = await asyncio.to_thread(wait_task, service, client, grant, task)
            assert done["state"] == "completed", done
            rejected = await remote.call_tool(
                "apply_change", {**params, "preview_id": data["id"], "request_id": "sdk-2", "confirmed": True}
            )
            assert rejected.is_error
            monitor = await remote.call_tool(
                "start_monitoring",
                {**params, "request": {"kind": "waveform", "points": 16}, "request_id": "disconnect-monitor"},
            )
            assert not monitor.is_error
            monitor_id = monitor.structured_content["data"]["id"]
        # Transport disconnect does not own the lifetime of accepted work.
        await asyncio.sleep(0.15)
        async with remote_client({"url": url}, True, token) as reconnected:
            result = await reconnected.call_tool(
                "get_task_status", {"grant_id": grant, "task_id": monitor_id}
            )
            assert result.structured_content["data"]["state"] == "running"
            stopped = await reconnected.call_tool(
                "stop_task", {"grant_id": grant, "task_id": monitor_id, "request_id": "disconnect-stop"}
            )
            assert not stopped.is_error
            await asyncio.to_thread(wait_task, service, client, grant, stopped.structured_content["data"])

    asyncio.run(run())


def test_request_conflict_channels_and_result_authority(experiment):
    _runtime, service, client, _token, grant, device = experiment
    task = service.acquire_once(client, grant, device, {"kind": "waveform", "points": 16}, "once")
    assert wait_task(service, client, grant, task)["state"] == "completed"
    with pytest.raises(DeviceError, match="request_id"):
        service.acquire_once(client, grant, device, {"kind": "waveform", "points": 32}, "once")
    with pytest.raises(DeviceError, match="通道"):
        service.acquire_once(client, grant, device, {"channels": [2]}, "other-channel")
    other, _ = service.policy.create_client("channel two")
    other_grant = service.policy.create_grant(
        other, [device], ["view", "results", "stop"], channels=[2], operations=[], limits={}
    )
    artifact = service.list_artifacts(client, grant, task["id"])[0]
    assert not service.list_artifacts(other, other_grant, task["id"])
    assert not service.list_tasks(other, other_grant)
    with pytest.raises(DeviceError, match="通道"):
        service.get_artifact(other, other_grant, artifact["id"])
    with pytest.raises(DeviceError, match="通道"):
        service.stop_task(other, other_grant, task["id"], "stop-other-channel")
    with pytest.raises(DeviceError):
        service.artifact_path(client, grant, "../../secret")


def test_expiration_and_cross_client_stop(experiment):
    runtime, service, client, _token, grant, device = experiment
    other, _ = service.policy.create_client("collaborator")
    other_grant = service.policy.create_grant(
        other, [device], ["view", "stop"], channels=[1], operations=[], limits={}
    )
    task = service.start_monitoring(client, grant, device, {"kind": "waveform", "points": 16}, "monitor")
    assert any(t["id"] == task["id"] for t in service.list_tasks(other, other_grant))
    stop = service.stop_task(other, other_grant, task["id"], "shared-stop")
    assert wait_task(service, other, other_grant, stop)["state"] == "completed"
    task = service.start_recording(client, grant, device, {"kind": "waveform", "points": 16}, "expires")
    service.policy.grants[grant]["expires_at"] = time.time() - 1
    service.wake.set()
    assert wait_task(service, other, other_grant, task)["state"] in {"completed", "failed"}
    assert not runtime.sessions.sessions[device].controls_locked
    assert not runtime.monitoring.tasks[device].running


def test_audit_failure_blocks_writes_but_allows_stop(experiment, monkeypatch):
    runtime, service, client, _token, grant, device = experiment
    task = service.start_monitoring(client, grant, device, {"kind": "waveform"}, "monitor-audit")
    preview = service.preview_change(client, grant, device, "timebase", {"value": 0.002})

    def failed(*args, **kwargs):
        raise OSError("audit unavailable")

    monkeypatch.setattr(service.repository, "audit", failed)
    with pytest.raises(OSError, match="audit unavailable"):
        service.apply_change(client, grant, device, preview["id"], "rejected-audit")
    stop = service.stop_task(client, grant, task["id"], "audit-stop")
    assert wait_task(service, client, grant, stop)["state"] == "completed"
    assert runtime.sessions.sessions[device].configuration_version == 0


def test_gui_task_explicit_stop_and_monitor_guard(experiment):
    from lab_control.domain.instruments import AcquisitionRequest

    runtime, service, client, _token, grant, device = experiment
    runtime.monitoring.start(device, (AcquisitionRequest(kind="waveform", points=16),))
    tasks = service.list_tasks(client, grant)
    gui = next(t for t in tasks if t["id"].startswith("gui-"))
    stop = service.stop_task(client, grant, gui["id"], "gui-stop")
    assert wait_task(service, client, grant, stop)["state"] == "completed"
    service.start_monitoring(client, grant, device, {"kind": "waveform", "points": 16}, "guard")
    monitor = runtime.monitoring.tasks[device]
    assert not monitor.requests[0].allow_setup
    service.policy.revoke_grant(grant)
    with pytest.raises(DeviceError, match="授权"):
        monitor.before_acquire(runtime.sessions.sessions[device], monitor.requests[0])


def test_read_only_connect_and_restart_never_replays(tmp_path):
    from lab_control.storage.agent_ledger import AgentLedger

    config = ConnectionConfig("mock://agent/restart", "rs.rtm3004", "mock")
    with ApplicationRuntime((config,), root=tmp_path, hardware=False) as runtime:
        device = runtime.add(config)
        runtime.connect(device).result()
        service = runtime.agent
        client, token = service.policy.create_client("restart")
        grant = service.policy.create_grant(
            client, [device], ["view", "connect", "disconnect"], channels=[1], operations=[], limits={}
        )
        runtime.sessions.disconnect(device)
        task = service.connect_device(client, grant, device, "read-only")
        assert wait_task(service, client, grant, task)["state"] == "completed"
        assert not runtime.sessions.sessions[device].acquisition_setup_enabled
        denied = service.connect_device(client, grant, device, "initialize", True)
        assert wait_task(service, client, grant, denied)["code"] == "permission_denied"
        unknown = {**task, "id": "lost-command", "request_id": "unknown", "state": "running"}
        service.ledger.insert(unknown, AgentLedger.fingerprint("unknown"))
    with ApplicationRuntime((config,), root=tmp_path, hardware=False) as runtime:
        service = runtime.agent
        assert service.policy.authenticate(token) == client
        assert not service.policy.grants[grant]["enabled"]
        assert service.ledger.get("lost-command")["state"] == "interrupted"
        assert not runtime.sessions.sessions


def test_stdio_connector_has_only_protocol_stdout(experiment, tmp_path):
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    _runtime, service, _client, token, _grant, _device = experiment
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    service.server = AgentServer(service)
    service.server.configure({"enabled": True, "local_port": port})
    path = tmp_path / "connection 中文.json"
    service.server.export_connection(path)

    async def run():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "lab_control", "--agent-connector", "--connection", str(path)],
            env={"LAB_CONTROL_AGENT_TOKEN": token},
        )
        async with Client(parameters, read_timeout_seconds=10) as client:
            assert len((await client.list_tools()).tools) == 18
            result = await client.call_tool("get_server_info", {})
            assert not result.is_error and result.structured_content["data"]["simulation"]

    asyncio.run(run())


def test_https_certificate_validation_and_download(experiment, tmp_path, monkeypatch):
    import ssl

    from lab_control.agent.certificates import prepare_certificate
    from lab_control.agent.connector import connection, download

    _runtime, service, client, token, grant, device = experiment
    ports = []
    for _ in range(2):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            ports.append(sock.getsockname()[1])
    service.server = AgentServer(service)
    # Listener plumbing tested entirely on loopback. Production configure() rejects
    # loopback as a LAN interface; no real network or physical device is involved.
    service.server.settings.update(
        enabled=True, local_port=ports[0], lan_enabled=True, lan_address="127.0.0.1", lan_port=ports[1]
    )
    service.server.start()
    path = tmp_path / "https.json"
    service.server.export_connection(path, lan=True)
    monkeypatch.setenv("LAB_CONTROL_AGENT_TOKEN", token)
    config, verify, token = connection(path)
    with httpx2.Client(verify=verify, trust_env=False) as http:
        assert http.post(config["url"], json={}).status_code == 401
        assert (
            http.post(
                config["url"], headers={"Authorization": "Bearer " + token, "Host": "evil.test"}, json={}
            ).status_code
            == 403
        )
        assert (
            http.post(
                config["url"], headers={"Authorization": "Bearer " + token}, content=b"x" * (1024 * 1024 + 1)
            ).status_code
            == 413
        )
    with httpx2.Client(trust_env=False) as http, pytest.raises(httpx2.ConnectError):
        http.post(config["url"], json={})
    wrong, _ = prepare_certificate(tmp_path / "other", "127.0.0.1")
    trust = ssl.create_default_context(cafile=str(wrong))
    with httpx2.Client(verify=trust, trust_env=False) as http, pytest.raises(httpx2.ConnectError):
        http.post(config["url"], json={})
    # The trusted certificate still must name the destination.
    with httpx2.Client(verify=verify, trust_env=False) as http, pytest.raises(httpx2.ConnectError):
        http.post(config["url"].replace("127.0.0.1", "localhost"), json={})
    task = service.acquire_once(client, grant, device, {"kind": "waveform", "points": 16}, "https-data")
    assert wait_task(service, client, grant, task)["state"] == "completed"
    artifact = service.list_artifacts(client, grant, task["id"])[0]
    output = tmp_path / "downloaded"
    assert download(config, verify, token, artifact["id"], grant, output)["bytes"] == artifact["size"]
    service.policy.revoke_client(client)
    with httpx2.Client(verify=verify, trust_env=False) as http:
        assert (
            http.get(
                config["url"].removesuffix("/mcp") + "/artifacts/" + artifact["id"],
                headers={"Authorization": "Bearer " + token},
            ).status_code
            == 401
        )


@pytest.mark.parametrize("adapter", ["keithley.2231a", "rigol.dg5504pro", "rs.rtm3004", "keysight.3458a"])
def test_cross_model_capabilities_and_output_authority(tmp_path, adapter):
    config = ConnectionConfig("mock://agent/cross-model", adapter, "mock")
    with ApplicationRuntime((config,), root=tmp_path, hardware=False) as runtime:
        device = runtime.add(config)
        runtime.connect(device, configure_acquisition=False).result()
        service = runtime.agent
        client, _ = service.policy.create_client("cross model")
        capabilities = runtime.sessions.capability_snapshot(device).channels[1]
        operations = [c.name for c in capabilities.operations if c.writable]
        actions = ["view", "apply", "acquire"]
        if adapter == "keysight.3458a":
            actions.append("output_trigger")
        grant = service.policy.create_grant(
            client, [device], actions, channels=[1], operations=operations, limits={}
        )
        metadata = service.get_capabilities(client, grant, device)
        assert metadata["snapshot"]["identity"]["model"]
        for capability in capabilities.operations:
            assert metadata["effects"]["1"][capability.name]["write"] == (
                "output_trigger" if capability.requires_confirmation else "configuration"
            )
        if "output" in operations:
            assert capabilities.get("output").requires_confirmation
            with pytest.raises(DeviceError, match="授权"):
                service.preview_change(client, grant, device, "output", {"value": True})
        request = service._request(
            client,
            grant,
            device,
            "acquire",
            {
                "kind": "waveform"
                if adapter == "rs.rtm3004"
                else "status"
                if adapter == "rigol.dg5504pro"
                else "scalar",
                "quantity": "frequency" if adapter == "rigol.dg5504pro" else "",
                "points": 8,
            },
        )
        assert not request.allow_setup
        task = service.acquire_once(
            client,
            grant,
            device,
            {"kind": request.kind, "quantity": request.quantity, "points": 8},
            "cross-model",
        )
        assert wait_task(service, client, grant, task)["state"] == "completed"


def test_local_panel_and_reusable_grant_template(experiment, qtbot, tmp_path):
    from lab_control.app import load_stylesheet
    from lab_control.ui.pages.agent_collaboration import AgentCollaborationPanel, GrantDialog

    runtime, service, client, _token, grant, _device = experiment
    panel = AgentCollaborationPanel(runtime)
    qtbot.addWidget(panel)
    panel.setStyleSheet(load_stylesheet())
    panel.resize(1180, 520)
    panel.show()
    assert not panel.enabled.isChecked() and not panel.service.server
    dialog = GrantDialog(service, client, template=service.policy.grants[grant])
    qtbot.addWidget(dialog)
    assert dialog.channels.text() == "1" and dialog.hours.value() == 2
    dialog.save()
    assert dialog.grant_id and dialog.grant_id != grant
    assert service.policy.grants[dialog.grant_id]["limits"]["timebase"]["value"] == [0.0001, 0.01]
    assert panel.grab().save(str(tmp_path / "agent-panel.png"))
    panel.timer.stop()
    panel.shutdown()


def test_firewall_plan_is_scoped_and_maintenance_never_runs_during_session(experiment):
    from pathlib import Path

    from lab_control.agent.firewall import firewall_plan, launch_firewall

    runtime, _service, _client, _token, _grant, _device = experiment
    settings = {"lan_enabled": True, "lan_address": "192.0.2.1", "lan_port": 8766}
    plan = firewall_plan(settings, Path(sys.executable))
    assert plan["remote_address"] == "LocalSubnet" and plan["profile"] == "Private,Domain"
    assert firewall_plan(settings, Path(sys.executable), remove=True)["rule"] == plan["rule"]
    with pytest.raises((ValueError, DeviceError, RuntimeError)):
        launch_firewall(runtime, settings)


def test_acquisition_setup_denial_does_not_initialize_shared_session(experiment, monkeypatch):
    from lab_control.domain.instruments import AcquisitionRequest

    runtime, _service, _client, _token, _grant, device = experiment
    session = runtime.sessions.sessions[device]
    prepared = []
    acquire = session.adapter.acquire
    calls = []

    def needs_setup(request):
        calls.append(request)
        if len(calls) == 1:
            raise DeviceError("setup needed", "acquisition_setup_required")
        return acquire(request)

    monkeypatch.setattr(session.adapter, "acquire", needs_setup)
    monkeypatch.setattr(
        runtime.sessions, "_prepare_acquisition_state", lambda _session: prepared.append(True) or True
    )
    with pytest.raises(DeviceError, match="setup needed"):
        runtime.sessions.operations.acquire(session, AcquisitionRequest(kind="waveform", allow_setup=False))
    assert not prepared and len(calls) == 1
    calls.clear()
    runtime.sessions.operations.acquire(session, AcquisitionRequest(kind="waveform", allow_setup=True))
    assert prepared == [True] and len(calls) == 2


def test_failed_grant_persistence_does_not_expand_authority(experiment, monkeypatch):
    _runtime, service, client, _token, grant, device = experiment
    before = service.policy.snapshot()

    def failed():
        raise OSError("database full")

    monkeypatch.setattr(service.policy, "_save", failed)
    with pytest.raises(OSError, match="database full"):
        service.policy.create_grant(
            client, [device], ["output_trigger"], channels=[1], operations=[], limits={}
        )
    with pytest.raises(OSError, match="database full"):
        service.policy.create_client("failed client")
    assert service.policy.snapshot() == before
    assert "output_trigger" not in service.policy.grants[grant]["actions"]


def test_shutdown_releases_resources_after_listener_failure(experiment):
    from types import SimpleNamespace

    runtime, service, _client, _token, _grant, _device = experiment

    def failed():
        raise OSError("listener shutdown failed")

    service.server = SimpleNamespace(close=failed)
    with pytest.raises(OSError, match="listener shutdown failed"):
        runtime.close()
    assert runtime.closed and not service.thread.is_alive() and service.worker._shutdown
    assert not runtime.sessions.sessions


def test_runtime_exit_drains_recording_and_persists_completion(experiment, tmp_path):
    runtime, service, client, _token, grant, device = experiment
    task = service.start_recording(
        client, grant, device, {"kind": "waveform", "points": 16}, "close-record", 0.05
    )
    deadline = time.monotonic() + 5
    while not runtime.monitoring.latest(device, "waveform"):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    runtime.close()
    with ApplicationRuntime(root=tmp_path, hardware=False) as reopened:
        saved = reopened.agent.ledger.get(task["id"])
        assert saved["state"] == "completed", saved
        assert saved["result"]["committed"] == saved["result"]["received"] > 0
        assert saved["result"]["converted"] == saved["result"]["committed"]
        assert reopened.agent.ledger.artifacts(task["id"])


def test_console_executable_always_dispatches_to_connector(monkeypatch):
    from lab_control import launcher
    from lab_control.agent import connector

    calls = []
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "C:/package/TF-LabControl-Agent.exe")
    monkeypatch.setattr(sys, "argv", [sys.executable])
    monkeypatch.setattr(connector, "main", lambda argv: calls.append(argv) or 2)
    assert launcher.main() == 2 and calls == [[]]
