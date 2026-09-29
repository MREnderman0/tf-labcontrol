"""Read standard USB descriptors from one selected Windows hub port, on explicit connection.

This is not a discovery provider. No device configuration, alternate interface, reset,
vendor request or bulk transfer is issued. The caller holds the selected device lease.
See Microsoft's USBView sample and USB_DESCRIPTOR_REQUEST documentation.
"""

import struct
import sys
from typing import Any, Callable
from uuid import UUID


def read_identity(instance: str, vendor: int, product: int) -> dict[str, Any]:
    if sys.platform != "win32":
        raise OSError("USB descriptor identity requires Windows")
    prefix = f"USB\\VID_{vendor:04X}&PID_{product:04X}\\"
    if not instance.upper().startswith(prefix) or not instance[len(prefix) :]:
        raise ValueError("Select an exact USB instance matching the expected VID/PID")
    evidence: dict[str, Any] = {"instance_id": instance, "requests": []}
    _read_selected(instance, vendor, product, evidence)
    return evidence


def _read_strings(ioctl: Callable, port: int, driver: str, vendor: int, product: int) -> dict[str, str]:
    # Verify this is the selected devnode before any device-level request.
    key = ioctl(0x220420, struct.pack("<I", port), 8192)  # GET_NODE_CONNECTION_DRIVERKEY_NAME
    if len(key) < 10 or struct.unpack_from("<I", key, 4)[0] > len(key):
        raise OSError("Incomplete USB port driver key")
    key_name = key[8:].decode("utf-16-le").split("\0")[0]
    if key_name.casefold() != driver.casefold():
        raise OSError("Selected USB port driver key changed")
    info = ioctl(0x220448, struct.pack("<I", port), 4096)  # GET_NODE_CONNECTION_INFORMATION_EX
    desc = info[4:22]
    if len(desc) != 18 or desc[:2] != b"\x12\x01":
        raise OSError("Invalid USB device descriptor")
    if struct.unpack_from("<HH", desc, 8) != (vendor, product):
        raise OSError("Selected USB port VID/PID changed")

    def descriptor(index: int, language: int) -> bytes:
        packet = struct.pack("<IBBHHH", port, 0x80, 6, 0x300 | index, language, 255)
        raw = ioctl(0x220410, packet, 267)  # GET_DESCRIPTOR_FROM_NODE_CONNECTION
        data = raw[12:]
        if len(data) < 2 or data[1] != 3 or data[0] < 2 or data[0] > len(data) or data[0] % 2:
            raise OSError("Invalid USB string descriptor")
        return data[2 : data[0]]

    languages = descriptor(0, 0)
    if len(languages) < 2:
        raise OSError("USB descriptor has no language ID")
    language = struct.unpack_from("<H", languages)[0]
    result = {}
    for label, offset in (("manufacturer", 14), ("product", 15), ("serial", 16)):
        if desc[offset]:
            value = descriptor(desc[offset], language).decode("utf-16-le")
            if not value or "\0" in value:
                raise OSError("Empty or embedded-NUL USB identity")
            result[label] = value
    if "serial" not in result:
        raise OSError("USB device exposes no serial descriptor")
    return result


def _read_selected(instance: str, vendor: int, product: int, evidence: dict) -> None:
    import ctypes as ct
    from ctypes import wintypes as wt

    cm = ct.WinDLL("cfgmgr32", use_last_error=True)
    kernel = ct.WinDLL("kernel32", use_last_error=True)
    cm.CM_Locate_DevNodeW.argtypes = [ct.POINTER(wt.DWORD), wt.LPWSTR, wt.ULONG]
    cm.CM_Get_Parent.argtypes = [ct.POINTER(wt.DWORD), wt.DWORD, wt.ULONG]
    cm.CM_Get_Device_IDW.argtypes = [wt.DWORD, wt.LPWSTR, wt.ULONG, wt.ULONG]
    cm.CM_Get_DevNode_Registry_PropertyW.argtypes = [
        wt.DWORD,
        wt.ULONG,
        ct.POINTER(wt.ULONG),
        ct.c_void_p,
        ct.POINTER(wt.ULONG),
        wt.ULONG,
    ]
    cm.CM_Get_Device_Interface_List_SizeW.argtypes = [ct.POINTER(wt.ULONG), ct.c_void_p, wt.LPCWSTR, wt.ULONG]
    cm.CM_Get_Device_Interface_ListW.argtypes = [ct.c_void_p, wt.LPCWSTR, wt.LPWSTR, wt.ULONG, wt.ULONG]
    kernel.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ct.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
    kernel.CreateFileW.restype = wt.HANDLE
    kernel.DeviceIoControl.argtypes = [
        wt.HANDLE,
        wt.DWORD,
        ct.c_void_p,
        wt.DWORD,
        ct.c_void_p,
        wt.DWORD,
        ct.POINTER(wt.DWORD),
        ct.c_void_p,
    ]
    kernel.DeviceIoControl.restype = wt.BOOL
    kernel.CloseHandle.argtypes = [wt.HANDLE]

    def check(result):
        if result:
            raise OSError("USB Configuration Manager result " + str(result))

    node, parent = wt.DWORD(), wt.DWORD()
    check(cm.CM_Locate_DevNodeW(ct.byref(node), ct.create_unicode_buffer(instance), 0))
    check(cm.CM_Get_Parent(ct.byref(parent), node, 0))
    parent_id = ct.create_unicode_buffer(4096)
    check(cm.CM_Get_Device_IDW(parent, parent_id, len(parent_id), 0))
    address, kind, size = wt.DWORD(), wt.ULONG(), wt.ULONG(4)
    check(
        cm.CM_Get_DevNode_Registry_PropertyW(node, 0x1D, ct.byref(kind), ct.byref(address), ct.byref(size), 0)
    )
    if kind.value != 4 or size.value != 4 or not 1 <= address.value <= 255:
        raise OSError("Invalid selected USB port")
    driver = ct.create_unicode_buffer(4096)
    size = wt.ULONG(ct.sizeof(driver))
    check(cm.CM_Get_DevNode_Registry_PropertyW(node, 0x0A, ct.byref(kind), driver, ct.byref(size), 0))
    if kind.value != 1 or not driver.value:
        raise OSError("Invalid selected USB driver key")
    guid = ct.create_string_buffer(UUID("f18a0e88-c30c-11d0-8815-00a0c906bed8").bytes_le)
    length = wt.ULONG()
    check(cm.CM_Get_Device_Interface_List_SizeW(ct.byref(length), guid, parent_id, 0))
    if not 2 <= length.value <= 32768:
        raise OSError("Invalid parent hub interface length")
    paths = ct.create_unicode_buffer(length.value)
    check(cm.CM_Get_Device_Interface_ListW(guid, parent_id, paths, length, 0))
    interfaces = [p for p in "".join(paths[:]).split("\0") if p]
    if len(interfaces) != 1:
        raise OSError("Selected USB parent hub interface absent or ambiguous")
    evidence.update(parent_id=parent_id.value, port=address.value, driver_key=driver.value, hub=interfaces[0])
    # FILE_ANY_ACCESS descriptor IOCTLs require no write access to the hub.
    handle = kernel.CreateFileW(interfaces[0], 0, 3, None, 3, 0, None)
    if handle == wt.HANDLE(-1).value:
        raise ct.WinError(ct.get_last_error())

    def ioctl(code: int, payload: bytes, capacity: int) -> bytes:
        buffer = ct.create_string_buffer(capacity)
        ct.memmove(buffer, payload, len(payload))
        count = wt.DWORD()
        if not kernel.DeviceIoControl(
            handle, code, buffer, len(buffer), buffer, len(buffer), ct.byref(count), None
        ):
            raise ct.WinError(ct.get_last_error())
        if count.value > capacity:
            raise OSError("Invalid USB response length")
        response = buffer.raw[: count.value]
        evidence["requests"].append(
            {"code": hex(code), "request_hex": payload.hex(), "response_hex": response.hex()}
        )
        return response

    try:
        evidence["strings"] = _read_strings(ioctl, address.value, driver.value, vendor, product)
        evidence["source"] = "Standard USB GET_DESCRIPTOR from selected parent hub port"
    finally:
        kernel.CloseHandle(handle)
