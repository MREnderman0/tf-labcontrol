"""Stdio/HTTPS bridge and authenticated artifact download; never opens hardware."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import ssl
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


def connection(path: Path) -> tuple[dict, ssl.SSLContext | bool, str]:
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    url = urlsplit(config["url"])
    if config.get("schema_version") != 1 or url.path != "/mcp" or url.query or url.fragment or url.username:
        raise ValueError("Invalid connection configuration")
    if url.scheme == "https":
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_verify_locations(cadata=config["certificate_pem"])
        verify: ssl.SSLContext | bool = context
    elif url.scheme == "http" and url.hostname == "127.0.0.1":
        verify = True
    else:
        raise ValueError("Remote connections require HTTPS with the exported server certificate")
    variable = config.get("token_env", "LAB_CONTROL_AGENT_TOKEN")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", variable):
        raise ValueError("Invalid token environment variable")
    token = os.environ.get(variable, "")
    if not token:
        raise ValueError(f"Set the credential environment variable {variable}")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise ValueError("Invalid credential format; copy the issued token without whitespace")
    return config, verify, token


@asynccontextmanager
async def remote_client(config, verify, token):
    async with httpx2.AsyncClient(
        verify=verify,
        trust_env=False,
        follow_redirects=False,
        headers={"Authorization": "Bearer " + token},
        timeout=70,
    ) as http:

        @asynccontextmanager
        async def transport():
            async with streamable_http_client(config["url"], http_client=http) as streams:
                yield streams[0], streams[1]

        async with Client(transport(), read_timeout_seconds=70) as client:
            yield client


async def bridge(config, verify, token, *, check=False):
    async with remote_client(config, verify, token) as client:
        if check:
            result = await client.call_tool("get_server_info", {})
            print(result.model_dump_json(by_alias=True))
            return 1 if result.is_error else 0

        async def list_tools(_context, params):
            return await client.list_tools(cursor=params.cursor if params else None, cache_mode="refresh")

        async def call_tool(_context, params):
            # The remote service owns accepted task lifetimes and request IDs.
            # Losing this stdio connection must not cancel its device work.
            return await client.call_tool(params.name, params.arguments or {})

        server = Server(
            "TF LabControl connector", version="1.0", on_list_tools=list_tools, on_call_tool=call_tool
        )
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    return 0


def download(config, verify, token, artifact: str, grant: str, output: Path):
    if not re.fullmatch(r"[a-f0-9]{32}", artifact) or not re.fullmatch(r"[a-f0-9]{32}", grant):
        raise ValueError("Invalid artifact or grant ID")
    url = config["url"].removesuffix("/mcp") + f"/artifacts/{artifact}?" + urlencode({"grant_id": grant})
    # Exclusive creation protects existing experiment files. A failed partial
    # download remains clearly named and is never presented as a complete result.
    if output.exists():
        raise FileExistsError(output)
    partial = output.with_name(output.name + ".partial")
    with httpx2.Client(
        verify=verify,
        trust_env=False,
        follow_redirects=False,
        headers={"Authorization": "Bearer " + token},
        timeout=70,
    ) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            count = 0
            with partial.open("xb") as stream:
                for chunk in response.iter_bytes():
                    stream.write(chunk)
                    count += len(chunk)
            expected = response.headers.get("content-length")
            if expected is not None and count != int(expected):
                raise OSError("Incomplete artifact download; partial file retained")
    # Windows rename refuses to replace an existing destination.
    partial.rename(output)
    return {"path": str(output.resolve()), "bytes": count}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="TF LabControl MCP connector (no instrument access)")
    parser.add_argument("--connection", required=True, type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--download", metavar="ARTIFACT_ID")
    parser.add_argument("--grant")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        config, verify, token = connection(args.connection)
        if args.download:
            if not args.grant or not args.output:
                parser.error("--download requires --grant and --output")
            print(
                json.dumps(
                    download(config, verify, token, args.download, args.grant, args.output),
                    ensure_ascii=False,
                )
            )
            return 0
        return asyncio.run(bridge(config, verify, token, check=args.check))
    except Exception as exc:
        print(f"Agent connector: {exc}", file=sys.stderr)
        return 2
