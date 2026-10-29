"""Authenticated local/LAN MCP listeners attached to one desktop runtime."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server.transport_security import RequestBodyLimitMiddleware, TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route

from lab_control.agent.certificates import prepare_certificate
from lab_control.agent.tools import create_mcp_server, principal
from lab_control.domain.instruments import DeviceError

DEFAULTS = {"enabled": False, "local_port": 8765, "lan_enabled": False, "lan_address": "", "lan_port": 8766}


class Authentication:
    def __init__(self, app, service, authority: str, scheme: str):
        self.app, self.service, self.authority, self.scheme = app, service, authority, scheme
        self.inflight = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope, receive)
        if request.headers.get("host") != self.authority or request.headers.get("origin") not in {
            None,
            f"{self.scheme}://{self.authority}",
        }:
            return await JSONResponse({"error": "invalid_host_or_origin"}, status_code=403)(
                scope, receive, send
            )
        credential = request.headers.get("authorization", "")
        try:
            if not credential.startswith("Bearer ") or len(credential) > 256:
                raise DeviceError("Unauthorized", "unauthorized")
            client = self.service.policy.authenticate(credential[7:])
        except DeviceError:
            return await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
        if self.inflight >= 32:
            return await JSONResponse({"error": "server_busy"}, status_code=429)(scope, receive, send)
        self.inflight += 1
        token = principal.set(client)
        try:
            return await self.app(scope, receive, send)
        finally:
            principal.reset(token)
            self.inflight -= 1


def create_app(service, host: str, port: int, *, tls=False):
    server = create_mcp_server(service)
    authority, scheme = f"{host}:{port}", "https" if tls else "http"
    mcp_app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        max_request_body_size=1024 * 1024,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[authority], allowed_origins=[f"{scheme}://{authority}"]
        ),
    )

    @asynccontextmanager
    async def lifespan(_app):
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    async def download(request):
        try:
            value, path = service.artifact_path(
                principal.get(), request.query_params.get("grant_id", ""), request.path_params["artifact_id"]
            )
            return FileResponse(
                path, filename=Path(value["name"]).name, headers={"Cache-Control": "no-store"}
            )
        except DeviceError as exc:
            return JSONResponse({"error": exc.code}, status_code=403)

    app = Starlette(
        routes=[Route("/artifacts/{artifact_id}", download), Mount("/", app=mcp_app)], lifespan=lifespan
    )
    return Authentication(RequestBodyLimitMiddleware(app, 1024 * 1024), service, authority, scheme)


class AgentServer:
    def __init__(self, service):
        self.service = service
        self.settings = {**DEFAULTS, **service.repository.load_workspace("agent/server", {})}
        self.thread: threading.Thread | None = None
        self.servers: list[Any] = []
        self.sockets: list[socket.socket] = []
        self.error = ""
        self.certificate: Path | None = None
        self._ready = threading.Event()

    def configure(self, settings):
        candidate = {**DEFAULTS, **settings}
        for name in ("local_port", "lan_port"):
            if type(candidate[name]) is not int or not 1024 <= candidate[name] <= 65535:
                raise ValueError("监听端口须为 1024..65535")
        if candidate["lan_enabled"]:
            from lab_control.discovery.lan import physical_ipv4_addresses

            address = ipaddress.ip_address(candidate["lan_address"])
            available = physical_ipv4_addresses()
            if address.is_loopback or address.is_unspecified or str(address) not in available:
                raise ValueError("请选择本机实验网卡上已配置的 IPv4 地址")
        self.close()
        self.settings = candidate
        self.service.repository.save_workspace("agent/server", candidate)
        if candidate["enabled"]:
            self.start()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.error = ""
        self._ready.clear()
        self.servers, self.sockets = [], []
        endpoints: list[tuple[str, int, Path | None, Path | None]] = [
            ("127.0.0.1", self.settings["local_port"], None, None)
        ]
        if self.settings["lan_enabled"]:
            address = self.settings["lan_address"]
            certificate_path, key_path = prepare_certificate(
                self.service.repository.root / "agent/tls", address
            )
            self.certificate = certificate_path
            endpoints.append((address, self.settings["lan_port"], certificate_path, key_path))
        try:
            for host, port, cert, key in endpoints:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                self.sockets.append(sock)
                sock.bind((host, port))
                sock.listen(64)
                sock.setblocking(False)
                config = uvicorn.Config(
                    create_app(self.service, host, port, tls=cert is not None),
                    host=host,
                    port=port,
                    access_log=False,
                    log_config=None,
                    log_level="warning",
                    ssl_certfile=str(cert) if cert else None,
                    ssl_keyfile=str(key) if key else None,
                    timeout_graceful_shutdown=5,
                )
                self.servers.append(uvicorn.Server(config))
        except Exception:
            for sock in self.sockets:
                sock.close()
            self.sockets.clear()
            self.servers.clear()
            raise
        self.thread = threading.Thread(target=self._run, name="agent-mcp", daemon=True)
        self.thread.start()
        if not self._ready.wait(10) or self.error:
            self.close()
            raise RuntimeError(self.error or "MCP 服务启动超时")

    def _run(self):
        async def serve():
            tasks = [
                asyncio.create_task(s.serve(sockets=[sock])) for s, sock in zip(self.servers, self.sockets)
            ]
            try:
                while not all(s.started for s in self.servers):
                    if any(t.done() for t in tasks):
                        raise RuntimeError("MCP 监听启动失败")
                    await asyncio.sleep(0.01)
                self._ready.set()
                await asyncio.gather(*tasks)
            finally:
                for s in self.servers:
                    s.should_exit = True
                await asyncio.gather(*tasks, return_exceptions=True)

        try:
            asyncio.run(serve())
        except Exception as exc:
            self.error = str(exc)
            self.service.repository.audit_sink.try_record("agent_server_failed", error=self.error)
        finally:
            self._ready.set()

    def snapshot(self):
        return {
            "running": bool(
                self.thread and self.thread.is_alive() and self._ready.is_set() and not self.error
            ),
            "settings": dict(self.settings),
            "error": self.error,
        }

    def export_connection(self, destination: Path, *, lan=False):
        if lan and (not self.settings["lan_enabled"] or self.certificate is None):
            raise ValueError("请先启用局域网接入")
        value = {
            "schema_version": 1,
            "url": (
                f"https://{self.settings['lan_address']}:{self.settings['lan_port']}/mcp"
                if lan
                else f"http://127.0.0.1:{self.settings['local_port']}/mcp"
            ),
            "token_env": "LAB_CONTROL_AGENT_TOKEN",
        }
        if lan:
            assert self.certificate is not None
            value["certificate_pem"] = self.certificate.read_text(encoding="ascii")
        with destination.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)

    def close(self):
        for server in self.servers:
            server.should_exit = True
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=15)
            if self.thread.is_alive():
                raise RuntimeError("MCP 服务仍在退出，请等待在途请求完成")
        for sock in self.sockets:
            sock.close()
        self.sockets.clear()
