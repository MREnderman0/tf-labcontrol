"""Read-only checks driven by component metadata; no model protocol or service startup."""

import hashlib
import os
from pathlib import Path


def inspect_files_usb(detection: dict, configs=()) -> tuple[str, str]:
    from lab_control.instruments.common.windows_usb import enumerate_usb

    candidates = [Path(c.server_path) for c in configs if c.server_path]
    candidates.extend(Path(os.path.expandvars(value)) for value in detection["executables"])
    executable = next((path for path in candidates if path.is_absolute() and path.is_file()), None)
    if executable is None:
        return "missing", "未找到已登记的厂商服务程序；使用离线材料安装"
    for file in detection["files"]:
        path = executable.parent / file["name"]
        if not path.is_file():
            return "missing", "缺少厂商组件：" + file["name"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != file["sha256"]:
            return "unknown", "厂商文件与登记版本哈希不一致，保留现有版本待核对：" + file["name"]
    rows = enumerate_usb(detection["usb_vendor"], detection["usb_product"])
    selected = {c.usb_instance_id.upper() for c in configs if c.usb_instance_id}
    if selected:
        rows = tuple(row for row in rows if row["PNPDeviceID"].upper() in selected)
    if not rows:
        return "unknown", "厂商程序哈希匹配；未枚举到所选 USB，驱动/设备状态待核对"
    if any(row["ConfigManagerErrorCode"] for row in rows):
        codes = sorted({row["ConfigManagerErrorCode"] for row in rows})
        return "missing", f"厂商程序哈希匹配；USB 驱动未就绪，系统错误码 {codes}"
    return "ok", "厂商程序哈希匹配、USB 驱动状态正常；未启动服务、通信或测量，硬件身份仍须核验"
