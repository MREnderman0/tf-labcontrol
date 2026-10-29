"""Versioned MCP tool inputs. Authentication identity never comes from arguments."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import Future
from contextvars import ContextVar
from typing import Annotated, Any, Literal

import mcp.types as types
from mcp.server.lowlevel import Server
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lab_control.domain.instruments import DeviceError, jsonable

principal: ContextVar[str] = ContextVar("agent_principal", default="")


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Grant(Empty):
    grant_id: str = Field(min_length=1, max_length=128)


class Device(Grant):
    device_id: str = Field(min_length=1, max_length=128)


class Request(Device):
    request_id: str = Field(min_length=1, max_length=128)


class Connect(Request):
    configure_acquisition: bool = False


class Read(Device):
    operations: list[str] = Field(min_length=1, max_length=32)
    channel: int = Field(default=1, ge=1)


class Preview(Device):
    operation: str = Field(min_length=1, max_length=128)
    parameters: dict[str, Any]
    channel: int = Field(default=1, ge=1)


class Apply(Request):
    preview_id: str


class Acquisition(Empty):
    kind: str = Field(
        default="scalar",
        max_length=128,
        description="Use a kind advertised by this model's data sources, e.g. waveform or scalar",
    )
    channels: list[Annotated[int, Field(ge=1)]] = Field(
        default_factory=lambda: [1], min_length=1, max_length=1024
    )
    points: int = Field(default=1000, ge=1, le=4_000_000)
    quantity: str = Field(default="", max_length=128)
    initiate: bool = False
    waveform_extent: Literal["default", "screen", "memory"] = "default"
    duration: float | None = Field(default=None, gt=0)
    sample_rate: float | None = Field(default=None, gt=0)


class Capture(Request):
    request: Acquisition


class Monitor(Capture):
    interval: float = Field(default=0.5, ge=0.05, le=60)


class Task(Grant):
    task_id: str


class Stop(Task):
    request_id: str = Field(min_length=1, max_length=128)


class Latest(Device):
    kind: str = "scalar"
    quantity: str = ""
    channel: int = Field(default=1, ge=1)


class Artifacts(Grant):
    task_id: str = ""


class Artifact(Grant):
    artifact_id: str


# name, input, description, cached/read-only. Device reads are conservatively
# marked as operations; annotations describe behavior, never enforce authority.
TOOLS = (
    ("get_server_info", Empty, "查看服务信息与本客户端的实验授权；先调用此工具。", True),
    ("list_devices", Grant, "列出实验授权内的设备和缓存状态；不扫描仪器。", True),
    ("get_capabilities", Device, "读取连接会话的能力、单位、通道、范围与副作用说明。", True),
    ("get_device_state", Device, "读取缓存的会话、监测和记录状态；输出未知不能解释为关闭。", True),
    ("connect_device", Connect, "连接指定资产或复用会话；默认跳过会改变设置的采集初始化。", False),
    ("disconnect_device", Request, "断开空闲设备；活动任务必须先明确停止并完成收尾。", False),
    ("read_parameters", Read, "通过设备原 worker 读取参数；需设备读取权限，记录期间可能拒绝。", False),
    ("preview_change", Preview, "校验并预览单项修改；五分钟有效，绑定会话及配置版本。", True),
    ("apply_change", Apply, "执行已预览且获授权的修改；request_id 防重，冲突需重新规划。", False),
    ("acquire_once", Capture, "采集一帧并保存，立即返回 task_id；不接受客户端确认标志。", False),
    ("start_monitoring", Monitor, "启动监测并返回任务；软件负责采集节奏，断线不停止。", False),
    ("start_recording", Monitor, "启动逐帧记录并返回任务；参数锁保留至二进制收尾完成。", False),
    ("list_tasks", Grant, "查看授权设备的近期任务，包括其他协作者创建的任务。", True),
    ("get_task_status", Task, "查看任务结果和记录收尾状态；中断或未知结果不得重发写入。", True),
    ("get_latest_measurement", Latest, "读取最近有效测量摘要及真实时间，不额外访问设备。", True),
    ("stop_task", Stop, "请求停止指定任务并排空记录；已接受命令不能撤回，输出不自动改变。", False),
    ("list_artifacts", Artifacts, "列出授权范围内的实验文件引用；无任意目录浏览能力。", True),
    ("get_artifact", Artifact, "获取产物元数据与带鉴权的下载路径；下载仍需本客户端凭据。", True),
)


def create_mcp_server(service) -> Server:
    schemas = {name: model for name, model, _description, _read in TOOLS}

    async def list_tools(_context, _params):
        service.policy.require_client(principal.get())
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=name,
                    description=description,
                    input_schema=model.model_json_schema(),
                    annotations=types.ToolAnnotations(
                        read_only_hint=read,
                        destructive_hint=not read,
                        idempotent_hint=read,
                        open_world_hint=False,
                    ),
                )
                for name, model, description, read in TOOLS
            ]
        )

    async def call_tool(_context, params):
        client = principal.get()
        try:
            if params.name not in schemas:
                raise DeviceError("工具不存在", "unsupported")
            arguments = (
                schemas[params.name].model_validate(params.arguments or {}).model_dump(exclude_unset=True)
            )
            service.policy.require_client(client)
            result = await asyncio.to_thread(getattr(service, params.name), client, **arguments)
            if isinstance(result, Future):
                result = await asyncio.shield(asyncio.wrap_future(result))
            payload = {"ok": True, "data": jsonable(result)}
            service.repository.audit_sink.try_record(
                "agent_tool_completed", client_id=client, tool=params.name, arguments=arguments
            )
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text", text=json.dumps(payload, ensure_ascii=False, allow_nan=False)
                    )
                ],
                structured_content=payload,
            )
        except (DeviceError, ValidationError, ValueError, KeyError, TypeError) as exc:
            service.repository.audit_sink.try_record(
                "agent_tool_rejected",
                client_id=client,
                tool=params.name,
                code=getattr(exc, "code", "invalid_parameter"),
            )
            payload = {"ok": False, "code": getattr(exc, "code", "invalid_parameter"), "message": str(exc)}
            return types.CallToolResult(
                is_error=True,
                content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
                structured_content=payload,
            )

    return Server(
        "TF LabControl",
        version="1.0",
        instructions=(
            "先查看授权与设备能力。仅操作明确选定的资产。写入先预览，使用唯一 request_id。"
            "超时或不确定结果先查询任务，禁止换 ID 重发。采集由软件调度，不用循环调用工具模拟采样。"
            "测量错误、模拟结果和未实测状态必须如实说明。"
        ),
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        get_tool_input_schema=lambda name: schemas[name].model_json_schema() if name in schemas else None,
    )
