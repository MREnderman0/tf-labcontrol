"""Explicit software-only protocol/connector acceptance, used by deployment checks."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from lab_control.agent.server import AgentServer
from lab_control.application.config import ConnectionConfig


def verify_mock(runtime, *, connector: Path | None = None, codex: Path | None = None) -> dict:
    if runtime.hardware or runtime.inventory.all():
        raise ValueError("Agent verification requires a new isolated Mock runtime")
    config = ConnectionConfig("mock://agent-acceptance/scope", "rs.rtm3004", "mock")
    device = runtime.add(config)
    runtime.connect(device, configure_acquisition=False).result(10)
    service = runtime.agent
    client, token = service.policy.create_client("isolated acceptance")
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
    runtime.sessions.disconnect(device)
    ports = []
    for _ in range(2):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            ports.append(sock.getsockname()[1])
    service.server = AgentServer(service)
    # Exercise real TLS on loopback without opening a LAN listener or firewall.
    service.server.settings.update(
        enabled=True, local_port=ports[0], lan_enabled=True, lan_address="127.0.0.1", lan_port=ports[1]
    )
    service.server.start()
    connection = runtime.repository.root / "connector.json"
    service.server.export_connection(connection, lan=True)
    if connector is None and getattr(sys, "frozen", False):
        connector = Path(sys.executable).with_name("TF-LabControl-Agent.exe")
    command = str(connector) if connector else sys.executable
    arguments = ([] if connector else ["-m", "lab_control"]) + [
        "--agent-connector",
        "--connection",
        str(connection),
    ]
    parameters = StdioServerParameters(
        command=command, args=arguments, env={"LAB_CONTROL_AGENT_TOKEN": token}
    )

    async def flow():
        async with Client(parameters, mode="legacy", read_timeout_seconds=20) as remote:
            assert len((await remote.list_tools()).tools) == 18

            async def call(name, **kwargs):
                result = await remote.call_tool(name, kwargs)
                assert not result.is_error, result
                return result.structured_content["data"]

            async def settled(task):
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    state = await call("get_task_status", grant_id=grant, task_id=task["id"])
                    if state["state"] == "completed":
                        return state
                    assert state["state"] in {
                        "accepted",
                        "running",
                        "stopping",
                        "binary_saved",
                        "converting",
                    }, state
                    await asyncio.sleep(0.05)
                raise TimeoutError("Mock task did not settle")

            assert (await call("get_server_info"))["simulation"]
            assert len(await call("list_devices", grant_id=grant)) == 1
            scope = {"grant_id": grant, "device_id": device}
            await settled(await call("connect_device", **scope, request_id="sdk-connect"))
            await call("get_capabilities", **scope)
            preview = await call("preview_change", **scope, operation="timebase", parameters={"value": 0.002})
            await settled(
                await call("apply_change", **scope, preview_id=preview["id"], request_id="sdk-apply")
            )
            monitoring = await call(
                "start_monitoring",
                **scope,
                request={"kind": "waveform", "points": 32},
                request_id="sdk-monitor",
                interval=0.05,
            )
            await asyncio.sleep(0.2)
            assert (await call("get_latest_measurement", **scope, kind="waveform"))["available"]
            await settled(
                await call(
                    "stop_task", grant_id=grant, task_id=monitoring["id"], request_id="sdk-stop-monitor"
                )
            )
            recording = await call(
                "start_recording",
                **scope,
                request={"kind": "waveform", "points": 32},
                request_id="sdk-record",
                interval=0.05,
            )
            await asyncio.sleep(0.3)
            await settled(
                await call("stop_task", grant_id=grant, task_id=recording["id"], request_id="sdk-stop-record")
            )
            await settled(recording)
            artifacts = await call("list_artifacts", grant_id=grant, task_id=recording["id"])
            artifact = next(item for item in artifacts if item["name"].endswith(".csv"))
            await call("get_artifact", grant_id=grant, artifact_id=artifact["id"])
            return artifact

    artifact = asyncio.run(flow())
    environment = {**os.environ, "LAB_CONTROL_AGENT_TOKEN": token}
    output = runtime.repository.root / "downloaded.csv"
    downloaded = subprocess.run(
        [command, *arguments, "--download", artifact["id"], "--grant", grant, "--output", str(output)],
        env=environment,
        capture_output=True,
        timeout=30,
    )
    assert downloaded.returncode == 0, downloaded.stderr.decode(errors="replace")
    assert output.stat().st_size == artifact["size"]
    result = {
        "passed": True,
        "hardware_accessed": False,
        "stdio": True,
        "tls": True,
        "legacy_protocol": True,
        "tools": 18,
        "download_bytes": artifact["size"],
    }
    if codex:
        task_prefix = "codex-acceptance-"
        prompt = (
            "Perform a software acceptance test using ONLY the lab_control MCP tools. This server is an "
            "isolated Mock and explicitly authorized. Do not use shell or edit files. Call get_server_info "
            "and list_devices, connect the authorized device, inspect capabilities, preview and apply timebase "
            "value 0.003 on channel 1. Then start waveform monitoring (points 32, interval 0.1), read a latest "
            "measurement and stop that task. Start waveform recording (points 32, interval 0.1), read latest "
            "measurement, stop, poll until completed, then list_artifacts and get_artifact for a CSV. Use "
            f"request_ids prefixed {task_prefix}. Verify simulation quality and final results. Report failures honestly."
        )
        overrides = [
            "mcp_servers.lab_control.command=" + json.dumps(command),
            "mcp_servers.lab_control.args=" + json.dumps(arguments),
            'mcp_servers.lab_control.env_vars=["LAB_CONTROL_AGENT_TOKEN"]',
            "mcp_servers.lab_control.startup_timeout_sec=30",
            "mcp_servers.lab_control.tool_timeout_sec=60",
            # Only this disposable Mock service is preapproved by the explicit
            # acceptance invocation. Never alter the user's real MCP settings.
            'mcp_servers.lab_control.default_tools_approval_mode="approve"',
            'approval_policy="never"',
        ]
        argv = [
            str(codex),
            "exec",
            "--ignore-user-config",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--json",
            "-C",
            str(runtime.repository.root),
        ]
        for override in overrides:
            argv.extend(["-c", override])
        argv.append(prompt)
        log = runtime.repository.root / "codex-events.jsonl"
        with log.open("wb") as stream, log.with_suffix(".stderr").open("wb") as errors:
            run = subprocess.run(argv, env=environment, stdout=stream, stderr=errors, timeout=300)
        tasks = [t for t in service.ledger.tasks() if t["request_id"].startswith(task_prefix)]
        result["codex"] = {
            "returncode": run.returncode,
            "tasks": [{"action": t["action"], "state": t["state"]} for t in tasks],
            "passed": run.returncode == 0
            and {"connect", "apply", "monitor", "record", "stop"}
            <= {t["action"] for t in tasks if t["state"] == "completed"},
            "log": str(log),
        }
        result["passed"] = result["codex"]["passed"]
    return result
