"""Windows USB PnP metadata only. No device handles, drivers or parameter commands."""

import re
import sys
from typing import Any


def enumerate_usb(vendor: str, product: str) -> tuple[dict[str, Any], ...]:
    if not all(re.fullmatch(r"[0-9A-Fa-f]{4}", value) for value in (vendor, product)):
        raise ValueError("USB vendor/product must be four hexadecimal digits")
    if sys.platform != "win32":
        return ()
    prefix = f"USB\\VID_{vendor}&PID_{product}\\".upper()
    return tuple(row for row in _present_devices() if row["PNPDeviceID"].upper().startswith(prefix))


def _present_devices() -> list[dict[str, Any]]:
    # SetupAPI reads PnP metadata even when the device has no driver. It creates
    # an information-set handle, never a device communication handle.
    # https://learn.microsoft.com/windows/win32/api/setupapi/nf-setupapi-setupdigetclassdevsw
    import ctypes as ct
    from ctypes import wintypes as wt

    class DeviceInfo(ct.Structure):
        _fields_ = [
            ("size", wt.DWORD),
            ("guid", ct.c_byte * 16),
            ("devinst", wt.DWORD),
            ("reserved", ct.c_size_t),
        ]

    setup = ct.WinDLL("setupapi", use_last_error=True)
    config = ct.WinDLL("cfgmgr32", use_last_error=True)
    setup.SetupDiGetClassDevsW.argtypes = [ct.c_void_p, wt.LPCWSTR, wt.HWND, wt.DWORD]
    setup.SetupDiGetClassDevsW.restype = wt.HANDLE
    setup.SetupDiEnumDeviceInfo.argtypes = [wt.HANDLE, wt.DWORD, ct.POINTER(DeviceInfo)]
    setup.SetupDiEnumDeviceInfo.restype = wt.BOOL
    setup.SetupDiGetDeviceInstanceIdW.argtypes = [
        wt.HANDLE,
        ct.POINTER(DeviceInfo),
        wt.LPWSTR,
        wt.DWORD,
        ct.POINTER(wt.DWORD),
    ]
    setup.SetupDiGetDeviceInstanceIdW.restype = wt.BOOL
    setup.SetupDiGetDeviceRegistryPropertyW.argtypes = [
        wt.HANDLE,
        ct.POINTER(DeviceInfo),
        wt.DWORD,
        ct.POINTER(wt.DWORD),
        ct.c_void_p,
        wt.DWORD,
        ct.POINTER(wt.DWORD),
    ]
    setup.SetupDiGetDeviceRegistryPropertyW.restype = wt.BOOL
    setup.SetupDiDestroyDeviceInfoList.argtypes = [wt.HANDLE]
    setup.SetupDiDestroyDeviceInfoList.restype = wt.BOOL
    config.CM_Get_DevNode_Status.argtypes = [ct.POINTER(wt.ULONG), ct.POINTER(wt.ULONG), wt.DWORD, wt.ULONG]
    config.CM_Get_DevNode_Status.restype = wt.ULONG
    handle = setup.SetupDiGetClassDevsW(None, "USB", None, 0x02 | 0x04)
    if handle == wt.HANDLE(-1).value:
        raise ct.WinError(ct.get_last_error())

    def text_property(info: DeviceInfo, prop: int) -> str:
        buffer = ct.create_unicode_buffer(4096)
        kind, required = wt.DWORD(), wt.DWORD()
        if not setup.SetupDiGetDeviceRegistryPropertyW(
            handle, ct.byref(info), prop, ct.byref(kind), buffer, ct.sizeof(buffer), ct.byref(required)
        ):
            error = ct.get_last_error()
            if error in (13, 1168):  # Missing optional property.
                return ""
            raise ct.WinError(error)
        if kind.value not in (1, 2):
            raise OSError("Unexpected USB text property type")
        return buffer.value

    rows = []
    try:
        index = 0
        while True:
            info = DeviceInfo(size=ct.sizeof(DeviceInfo))
            if not setup.SetupDiEnumDeviceInfo(handle, index, ct.byref(info)):
                error = ct.get_last_error()
                if error == 259:
                    break
                raise ct.WinError(error)
            index += 1
            instance = ct.create_unicode_buffer(4096)
            if not setup.SetupDiGetDeviceInstanceIdW(handle, ct.byref(info), instance, len(instance), None):
                raise ct.WinError(ct.get_last_error())
            status, problem = wt.ULONG(), wt.ULONG()
            result = config.CM_Get_DevNode_Status(ct.byref(status), ct.byref(problem), info.devinst, 0)
            if result:
                raise OSError(f"CM_Get_DevNode_Status failed: {result}")
            rows.append(
                {
                    "Name": text_property(info, 12) or text_property(info, 0),
                    "PNPDeviceID": instance.value,
                    "ConfigManagerErrorCode": problem.value,
                    "Status": "Error" if problem.value else "OK",
                    "Service": text_property(info, 4),
                }
            )
    finally:
        setup.SetupDiDestroyDeviceInfoList(handle)
    return rows
