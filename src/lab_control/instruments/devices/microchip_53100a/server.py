"""A session-owned TSERVE child. Scanning never constructs this object."""

import ctypes
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from lab_control.application.config import ConnectionConfig
from lab_control.domain.instruments import DeviceError
from lab_control.storage.repository import application_home

from .discovery import enumerate_devices
from .identity import verify_usb_identity


def vendor_processes() -> list[dict]:
    from lab_control.deployment.windows import powershell

    return powershell(
        "@{processes=@(Get-CimInstance Win32_Process -Filter "
        "\"Name='tserve64.exe' OR Name='tserve.exe' OR Name='timelab64.exe' OR Name='timelab.exe'\""
        " | Select-Object ProcessId,Name,CommandLine)} | ConvertTo-Json -Compress"
    )["processes"]


def find_server(explicit: str = "") -> Path:
    paths = (
        [Path(explicit)]
        if explicit
        else [
            Path(os.environ.get(key, fallback)) / vendor / "TimeLab/tserve64.exe"
            for key, fallback in (
                ("ProgramFiles", r"C:\Program Files"),
                ("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            )
            for vendor in ("Microchip", "Miles Design")
        ]
    )
    for path in paths:
        if path.is_absolute() and path.name.lower() == "tserve64.exe" and path.is_file():
            return path
    raise DeviceError("未找到 TimeLab TSERVE64；请安装已验证的离线组件或指定绝对路径", "driver_missing")


def listener_pids(port: int) -> set[int]:
    """Read Windows' IPv4 TCP owner table; no network packets are sent."""
    from ctypes import wintypes

    class Row(ctypes.Structure):
        _fields_ = [(name, wintypes.DWORD) for name in ("state", "local", "port", "remote", "rport", "pid")]

    api = ctypes.WinDLL("iphlpapi", use_last_error=True).GetExtendedTcpTable
    api.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.BOOL,
        wintypes.ULONG,
        ctypes.c_int,
        wintypes.ULONG,
    ]
    api.restype = wintypes.DWORD
    size = wintypes.DWORD()
    code = api(None, ctypes.byref(size), False, 2, 3, 0)
    if code not in (0, 122):
        raise OSError(code, "Cannot inspect TCP listener owners")
    buffer = ctypes.create_string_buffer(size.value)
    code = api(buffer, ctypes.byref(size), False, 2, 3, 0)
    if code:
        raise OSError(code, "Cannot inspect TCP listener owners")
    count = wintypes.DWORD.from_buffer(buffer).value
    if count > (len(buffer) - 4) // ctypes.sizeof(Row):
        raise OSError("Invalid TCP owner table")
    rows = (Row * count).from_buffer(buffer, 4)
    return {row.pid for row in rows if socket.ntohs(row.port & 0xFFFF) == port}


class ManagedServer:
    def __init__(self, config: ConnectionConfig) -> None:
        self.config = config
        self.process: subprocess.Popen | None = None
        self.log: BinaryIO | None = None
        self.folder: Path | None = None
        self.port = 0
        self.data_port = 0

    def verify_identity(self) -> dict:
        evidence = verify_usb_identity(self.config.usb_instance_id, self.config.expected_serial)
        if self.folder:
            (self.folder / "usb-identity.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return evidence

    def start(self) -> str:
        if sys.platform != "win32":
            raise DeviceError("Local TSERVE management requires Windows", "driver_missing")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{1,127}", self.config.expected_serial):
            raise DeviceError("请先核对并填写设备序列号；USB 标识不能替代仪器序列号", "identity_required")
        matches = [
            row
            for row in enumerate_devices()
            if row["PNPDeviceID"].upper() == self.config.usb_instance_id.upper()
        ]
        if len(matches) != 1:
            raise DeviceError("选定的 53100A USB 设备未连接", "disconnected")
        if matches[0].get("ConfigManagerErrorCode") != 0:
            raise DeviceError("53100A USB 驱动未就绪", "driver_missing")
        for process in vendor_processes():
            command = process.get("CommandLine") or ""
            bound = re.search(r"/serial:([A-Za-z0-9._:/-]+)", command, re.I)
            if (
                "timelab" in process["Name"].lower()
                or bound is None
                or bound[1].lower() == "any"
                or bound[1] == self.config.expected_serial
            ):
                raise DeviceError(
                    "已有 TimeLab/TSERVE 可能占用目标；请先在原程序正常释放设备", "resource_busy"
                )
        executable = find_server(self.config.server_path)
        from lab_control.deployment.windows import signature_status

        signature = signature_status(executable)
        if signature.get("status") != "Valid" or "Miles Design" not in signature.get("subject", ""):
            raise DeviceError("TSERVE64 签名无法确认；保留现有程序并检查离线组件", "driver_incompatible")
        with socket.socket() as reservation, socket.socket() as data_reservation:
            reservation.bind(("127.0.0.1", 0))
            self.port = reservation.getsockname()[1]
            data_reservation.bind(("127.0.0.1", 0))
            self.data_port = data_reservation.getsockname()[1]
        self.folder = application_home() / "tserve" / uuid4().hex
        self.folder.mkdir(parents=True, exist_ok=False)
        self.log = (self.folder / "console.log").open("wb")
        arguments = [
            str(executable),
            "/serial:" + self.config.expected_serial,
            f"/cmd_port:{self.port}",
            f"/data_port:{self.data_port}",
            "/format:P",
            "/timestamp:s",
            "/phasedec:2",
            "/sep:32",
            "/ch:3-1",
            "/smooth:0",
            "/notify",
            "/logfile:" + str(self.folder / "server.log"),
        ]
        # Never /start, /update, output filenames, firmware commands or network repair.
        try:
            self.verify_identity()
            self.process = subprocess.Popen(
                arguments,
                cwd=executable.parent,
                stdin=subprocess.DEVNULL,
                stdout=self.log,
                stderr=subprocess.STDOUT,
                creationflags=0x08000000,
            )
            (self.folder / "owner.json").write_text(
                json.dumps(
                    {
                        "pid": self.process.pid,
                        "arguments": arguments,
                        "usb_instance_id": self.config.usb_instance_id,
                        "measurement_started": False,
                        "signature": signature,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            deadline = time.monotonic() + max(10, self.config.timeout)
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise DeviceError("TSERVE 启动失败；见运行目录服务日志", "connection_failed")
                owners = listener_pids(self.port)
                data_owners = listener_pids(self.data_port)
                if owners == data_owners == {self.process.pid}:
                    return f"tcp://127.0.0.1:{self.port}"
                if any(pids and pids != {self.process.pid} for pids in (owners, data_owners)):
                    raise DeviceError("TSERVE 端口被另一进程占用", "resource_busy")
                time.sleep(0.1)
            raise DeviceError("TSERVE 启动超时", "timeout")
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        process = self.process
        abnormal = process is not None and process.poll() is not None and process.returncode != 0
        if process is not None and process.poll() is None:
            # Shutdown is sent only to our recorded child's verified listener.
            try:
                if listener_pids(self.port) == {process.pid}:
                    from .transport import TserveTransport

                    control = TserveTransport("127.0.0.1", self.port, 2, b"\r\n")
                    try:
                        control.open()
                        control.write("shutdown")
                        # Keep the client open until the server processes shutdown;
                        # closing with unread replies can produce TCP RST and lose it.
                        process.wait(timeout=3)
                    finally:
                        control.close()
            except (OSError, DeviceError, subprocess.TimeoutExpired):
                pass
            if process.poll() is None:
                # The Popen handle refers to this exact child, never a process-name search.
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired as exc:
                    raise DeviceError(
                        "项目创建的 TSERVE 尚未退出，设备占用未确认释放", "cleanup_failed"
                    ) from exc
                if self.folder:
                    (self.folder / "forced-exit.json").write_text(
                        json.dumps(
                            {
                                "pid": process.pid,
                                "reason": "Graceful shutdown did not complete",
                                "measurement_state": "unknown",
                                "returncode": process.returncode,
                            }
                        ),
                        encoding="utf-8",
                    )
                abnormal = True
        self.process = None
        if self.log:
            self.log.close()
            self.log = None
        if abnormal:
            raise DeviceError(
                "TSERVE 异常退出或需要强制结束；详见服务日志，设备状态未核验", "server_exit_abnormal"
            )
