# TimeLab / TSERVE / USB 驱动材料

组件 ID：`tserve`。Windows 10/11 x64；普通运行不提权。2026-09-28 在开发机安装材料并核验，逐资产结果只保存在运行目录，不能代替干净目标电脑验收。

| 项目 | 登记值 |
| --- | --- |
| 正式版 | TimeLab 1.780A，2025-12-04 |
| 厂家资料 | [Microchip 用户手册](https://ww1.microchip.com/downloads/aemDocuments/documents/FTD/ProductDocuments/UserGuides/53100A-Phase-Noise-Analyzer-Users-Guide-DS50002991.pdf)，[Miles Design 版本说明](https://www.miles.io/timelab/readme.htm) |
| 官方直连 | [Microchip 专用安装包](https://www.miles.io/timelab/mchp/setup.exe)；URL 可能更新，缓存以登记哈希为准 |
| 离线文件 | `timelab-1.780a.exe`，51,742,336 bytes；下载进程不使用代理 |
| SHA256 | `a4eac3cace9eb52f648ea5b25d0c8e5ae5d02874429e33fc3215b8b6e33dc538` |
| 安装包签名 | Authenticode Valid；Miles Design LLC |
| 默认位置 | `C:\Program Files\Microchip\TimeLab`；同时保留旧 Miles Design 目录检测 |
| 安装 | Inno Setup `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /NOCLOSEAPPLICATIONS /NORESTARTAPPLICATIONS /RESTARTEXITCODE=3010`，另指定运行批次 `/LOG` |
| 安装边界 | 安装程序退出 0 仅证明应用安装成功；内置 pssetup 只向驱动库暂存材料，源码返回 1 表示 SetupCopyOEMInf 成功；必须单独核对实际设备错误码 |
| 驱动绑定 | 已连接且缺驱动的选定设备可用系统 PnPUtil 对准确 INF 执行 `/add-driver <INF> /install`；先持有排他维护锁，核实只有选定匹配设备，不使用 `/reboot`、强制降级或删除 |
| USB 驱动 | `USB Driver/53100A/wlh/x64/MD53100A.inf`；DriverVer 2017-12-24 / 3.4.7.1；VID 16D0 / PID 05B8；CAT/SYS 签名 Valid / Miles Design LLC |
| 权限/重启 | 安装及绑定单独 UAC；普通 TSERVE 不提权；任何重启要求只记录，由用户安排 |
| Windows 驱动策略兼容性 | 已发现 Windows Driver Policy 强制模式拒绝加载随包 3.4.7.1 驱动：PnP 错误 39、状态 `0xC0E90002`，CodeIntegrity 3077 的策略 GUID 为 `{8f9cb695-5d48-48d6-a329-7202b44607e3}`；安装退出 0、Authenticode Valid 及文件哈希一致均不能证明内核允许加载。依据：[微软驱动策略](https://support.microsoft.com/en-us/windows/hardware/drivers/the-windows-driver-policy) |
| 新版材料复核 | 2026-10-10 解包核对官方 TimeLab 1.79 Beta（2026-08-25），x64 MD53100A INF/CAT/SYS 的 SHA256 与下表全部一致，未提供可解决此阻塞的不同驱动；仅检查材料，未升级应用 |
| 此类失败的维护边界 | 保留应用、已暂存驱动和日志，标记 USB 未就绪；优先寻找厂家/Windows Update 的 WHCP 签名驱动。不得把证书到期单独判为根因，不把内存完整性与该策略混淆；安装器不自动移除系统安全策略、重签驱动、强制重启或重复安装相同材料 |
| 分发与发布 | 重新分发授权尚待取得/核实；许可、干净 Windows 10/11 离线修复及逐设备验收尚未完成，严格发布保持阻断 |

已核验安装文件 SHA256：

| 文件 | SHA256 | 签名 |
| --- | --- | --- |
| tserve64.exe | `05469f84d78c6cd9c2988feb7a9a5cf3577404091cf4704ee7716d6a8b0f1504` | Valid / Miles Design LLC |
| PhaseStation_64.tll | `53b21d8b4ad8194cdb795fa998af3d413ceb9555197562f9d16be1dffc436947` | 无独立签名；来源为已验签安装包，校验文件哈希 |
| MD53100A.inf | `2b3b4df27b8e76e7ca83d09b0902c2e927f22a352bf5e3492754c17a0daf125c` | 通过配套 CAT 校验；不是独立 Authenticode 文件 |
| MD53100A.cat | `bc242e13d77416260dc02530103882e5f0942ddc4dfa555dc3e87496092891d8` | Valid / Miles Design LLC |
| MD53100A.sys | `97c93789273ab3d2da624a0917be5a1244fe1cfd678fd5cd8fa3b267732db912` | Valid / Miles Design LLC |

系统检查只核对安装文件哈希和所选 USB 状态，不启动服务或仪器。安装材料在仓库缓存 `deployment/packages/installers`、开发临时缓存 `.tools/installers` 与本机维护缓存保留；恢复仅使用同版本已验证材料，不卸载共享驱动、不删除 TimeLab 或项目用户数据。

当前托管会话使用独立命令/数据临时 TCP 端口，均核验属于本次子进程；客户端仅使用 IPv4 回环地址。不创建防火墙规则。厂商 TSERVE 本身的监听地址策略未改动，不将客户端使用回环地址声称为服务仅监听回环。数据格式、时间戳、分隔符、抽取和单路配置由型号启动参数固定；启动服务仍不开始测量。无需新增 Python 包或 DLL，沿用登记的 TimeLab 1.780A 材料；新增采集功能的带信号验证仍待完成。

## 外部 PowerShell 驱动修复门禁

应用内维护进程不能把 UAC 提权、驱动暂存和内核加载混成一个成功状态。需要重新绑定时，从项目根目录启动独立的 Windows PowerShell；脚本会在需要时再次弹出 UAC，只接受本次明确选定的完整 USB InstanceId：

```powershell
$id = (Get-PnpDevice -PresentOnly |
    Where-Object InstanceId -like 'USB\VID_16D0&PID_05B8\*').InstanceId
if (@($id).Count -ne 1) { throw '请先只连接并选定一台 53100A' }
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\repair_53100a_driver.ps1 `
    -InstanceId $id -CycleDevice
```

`scripts/repair_53100a_driver.ps1` 在维护全程取得与源码 GUI、CLI 和 EXE 共用的硬件进程锁，逐项核对 INF/CAT/SYS 哈希和签名，并检查 TimeLab/TSERVE 占用；执行 `pnputil /add-driver ... /install`，可只对该 InstanceId 做一次禁用/启用，然后重新读取 `ConfigManagerErrorCode`。固定用户锁不随 `LAB_CONTROL_HOME` 变化；已有占用时退出并留档，不终止占用进程。结果、PnP 输出和 Code Integrity 事件默认保存在运行目录 `hardware-tests/53100A-selected-usb/<批次>-driver-repair`，可用 `-OutputRoot` 指定；运行目录遵循 `LAB_CONTROL_HOME`，否则使用 `%LOCALAPPDATA%\TF-LabControl`。提权支持中文及空格路径。Problem 39、45 或 `0xC0E90002` 必须保持失败并留证，不能用 PnPUtil 退出码 0 代替实际加载结果。

脚本不会删除驱动、降级共享驱动、重启电脑、关闭 Secure Boot、修改 Code Integrity/WDAC 或自动信任证书。若精确设备仍为 Problem 39，应向厂家索取适用于当前 Windows 驱动策略的 WHCP/Attestation 版本；设备未枚举时记录待连接，不绑定其他 USB 设备。序列号和完整 InstanceId 只写运行批次，不写入源码或组件清单。

随包 `tserve.cpp` 的 `CMD_show_serial` 仅在存在活动采集时读取硬件 Device ID；空闲返回 `Error: No active connection`。`CMD_show_state` 的 Ready 只表示系统存在相同 VID/PID 的 USB，并不核对序列号或占用。`show title` 也需要测量且可修改，不用于身份。

本机解法已实测：依据官方 `timelab.cpp` 的 `USBDEVICE::serial`，对实际读回的 USB 序列号去除冒号或空格后的附加字段。使用微软标准 [USB_DESCRIPTOR_REQUEST](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/usbioctl/ns-usbioctl-_usb_descriptor_request) 和 [USBView 示例](https://github.com/microsoft/Windows-driver-samples/tree/main/usb/usbview)的设备描述符读取方法，只请求已选择父集线器端口；厂家/完整型号/序列号必须全部匹配。普通权限实测成功，无新增 DLL 或 Python 包。身份来源为 USB 描述符；TSERVE 直接硬件身份、固件及采集仍另行判定。

未采用的官方途径：`PSLockDevice` 会遍历设备且调用设置备用接口；`EventAcqSetup` 需要启动采集流程；`selftest` 会执行硬件自检。它们都不符合本轮仅只读身份核验的边界。1.79 beta 发行说明没有列出空闲 TSERVE 身份修复，因此保持已验证的正式版。
