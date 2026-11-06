"""Execute an explicitly enabled local experiment command with JSON pipes."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import shlex
import subprocess
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from lab_control.domain.instruments import DeviceError, jsonable
from lab_control.domain.metrics import MetricInput, MetricParameters
from lab_control.storage.metric_results import MAX_RESULT_BYTES

LOCAL_COMMAND_PROFILE = "local.command"
COMMAND_VERSION = "1"
COMMAND_TIMEOUT = 300.0
PIPE_CHUNK = 64 * 1024
STDERR_LIMIT = 4096


class _CommandMetric(MetricParameters):
    value: float | int | None
    unit: str = Field(max_length=64)
    quality: Literal["good", "simulated", "unavailable"]


class _CommandOutput(MetricParameters):
    metrics: dict[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")], _CommandMetric] = Field(
        min_length=1, max_length=128
    )


def command_capability() -> dict:
    return {
        "id": LOCAL_COMMAND_PROFILE,
        "version": COMMAND_VERSION,
        "description": "临时本机命令处理，输入规范记录，输出 stdout 指标 JSON",
        "parameters": {"type": "object"},
        "metrics": [],
        "requires_command": True,
        "supports_multiple_recordings": True,
        "supports_partial": False,
        "automatic_capture": False,
        "capture_parameters": None,
    }


def _split_command(command: str) -> tuple[str, ...]:
    command = command.strip()
    if not command or "\x00" in command or "\n" in command or "\r" in command:
        raise DeviceError("command 须为一行非空命令", "invalid_parameter")
    if os.name != "nt":
        return tuple(shlex.split(command))

    split = ctypes.windll.shell32.CommandLineToArgvW
    split.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int))
    split.restype = ctypes.POINTER(wintypes.LPWSTR)
    count = ctypes.c_int()
    arguments = split(command, ctypes.byref(count))
    if not arguments:
        raise DeviceError("无法解析 Windows 命令行", "invalid_parameter")
    try:
        result = tuple(arguments[: count.value])
    finally:
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = (wintypes.HLOCAL,)
        free.restype = wintypes.HLOCAL
        free(ctypes.cast(arguments, wintypes.HLOCAL))
    if not result or not result[0]:
        raise DeviceError("command 须指定可执行程序", "invalid_parameter")
    if Path(result[0]).suffix.lower() in {".bat", ".cmd"}:
        raise DeviceError("批处理文件须显式指定 cmd.exe 作为可执行程序", "invalid_parameter")
    return result


@contextmanager
def _windows_job(pid: int):
    if os.name != "nt":
        yield
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel.TerminateJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # PROCESS_SET_QUOTA | PROCESS_TERMINATE, for this invocation's child.
        child = kernel.OpenProcess(0x0100 | 0x0001, False, pid)
        if not child:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel.AssignProcessToJobObject(job, child):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.CloseHandle(child)
        try:
            yield
        finally:
            kernel.TerminateJobObject(job, 1)
    finally:
        kernel.CloseHandle(job)


def _input_parts(source: MetricInput, parameters: dict):
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"), default=jsonable)
    header = {
        "recording_id": source.recording_id,
        "recording_ids": source.recording_ids,
        "metadata": source.metadata,
        "complete": source.complete,
    }
    yield '{"parameters":'
    yield from encoder.iterencode(parameters)
    yield ',"source":'
    yield encoder.encode(header)[:-1]
    yield ',"frames":['
    for index, frame in enumerate(source.frames()):
        if index:
            yield ","
        yield from encoder.iterencode(frame)
    yield "]}}\n"


async def _feed(stream: asyncio.StreamWriter, source: MetricInput, parameters: dict) -> bool:
    buffer = bytearray()
    try:
        for part in _input_parts(source, parameters):
            buffer.extend(part.encode("utf-8"))
            if len(buffer) >= PIPE_CHUNK:
                source.check_cancel()
                stream.write(bytes(buffer))
                buffer.clear()
                await stream.drain()
                await asyncio.sleep(0)
        if buffer:
            stream.write(bytes(buffer))
            await stream.drain()
        return True
    except (BrokenPipeError, ConnectionResetError):
        return False
    finally:
        stream.close()


async def _read(stream: asyncio.StreamReader, limit: int, *, truncate: bool = False) -> bytes:
    buffer = bytearray()
    while chunk := await stream.read(PIPE_CHUNK):
        remaining = limit - len(buffer)
        if len(chunk) > remaining and not truncate:
            raise DeviceError("命令 stdout 超过 256 KiB", "command_output_limit")
        buffer.extend(chunk[:remaining])
    return bytes(buffer)


@dataclass(frozen=True)
class LocalCommand:
    argv: tuple[str, ...]
    cwd: Path

    id = LOCAL_COMMAND_PROFILE
    version = COMMAND_VERSION
    supports_multiple_recordings = True
    supports_partial = False
    capture = None
    capture_parameters = None

    @classmethod
    def parse(cls, command: str, cwd: Path) -> LocalCommand:
        return cls(_split_command(command), cwd)

    def process(self, source: MetricInput, parameters: dict) -> dict:
        try:
            output = asyncio.run(self._run(source, parameters))
        except OSError as exc:
            raise DeviceError("本机命令启动或管道通信失败：" + str(exc), "command_io_failed") from exc
        try:
            result = _CommandOutput.model_validate_json(output)
            if any(
                (item.value is None) != (item.quality == "unavailable") for item in result.metrics.values()
            ):
                raise ValueError("Invalid metric quality")
        except ValueError as exc:
            raise DeviceError("stdout 必须为一条有效的指标 JSON", "command_invalid_output") from exc
        return result.model_dump()["metrics"]

    async def _run(self, source: MetricInput, parameters: dict) -> bytes:
        source.check_cancel()
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.subprocess_exec(
            lambda: asyncio.subprocess.SubprocessStreamProtocol(limit=PIPE_CHUNK, loop=loop),
            *self.argv,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        tasks: set[asyncio.Task] = set()
        try:
            with _windows_job(transport.get_pid()):
                process = asyncio.subprocess.Process(transport, protocol, loop)
                assert process.stdin and process.stdout and process.stderr
                sent = asyncio.create_task(_feed(process.stdin, source, parameters))
                stdout = asyncio.create_task(_read(process.stdout, MAX_RESULT_BYTES))
                stderr = asyncio.create_task(_read(process.stderr, STDERR_LIMIT, truncate=True))
                exited = asyncio.create_task(process.wait())
                tasks = {sent, stdout, stderr, exited}
                pending = tasks
                deadline = loop.time() + COMMAND_TIMEOUT
                while pending:
                    source.check_cancel()
                    if loop.time() >= deadline:
                        raise DeviceError("本机命令执行超时", "command_timeout")
                    done, pending = await asyncio.wait(
                        pending, timeout=0.1, return_when=asyncio.FIRST_COMPLETED
                    )
                    for finished in done:
                        finished.result()
                source.check_cancel()
                if exited.result():
                    detail = stderr.result().decode("utf-8", errors="replace").strip()
                    message = f"本机命令退出码 {exited.result()}"
                    raise DeviceError(f"{message}：{detail}" if detail else message, "command_failed")
                if not sent.result():
                    raise DeviceError("命令在接收完整 stdin JSON 前关闭了管道", "command_input_closed")
                return stdout.result()
        finally:
            transport.close()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
