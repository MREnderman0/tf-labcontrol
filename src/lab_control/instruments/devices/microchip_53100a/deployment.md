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
| 分发与发布 | 重新分发授权尚待取得/核实；许可、干净 Windows 10/11 离线修复及逐设备验收尚未完成，严格发布保持阻断 |

已核验安装文件 SHA256：

| 文件 | SHA256 | 签名 |
| --- | --- | --- |
| tserve64.exe | `05469f84d78c6cd9c2988feb7a9a5cf3577404091cf4704ee7716d6a8b0f1504` | Valid / Miles Design LLC |
| PhaseStation_64.tll | `53b21d8b4ad8194cdb795fa998af3d413ceb9555197562f9d16be1dffc436947` | 无独立签名；来源为已验签安装包，校验文件哈希 |
| MD53100A.inf | `2b3b4df27b8e76e7ca83d09b0902c2e927f22a352bf5e3492754c17a0daf125c` | 通过配套 CAT 校验；不是独立 Authenticode 文件 |
| MD53100A.cat | `bc242e13d77416260dc02530103882e5f0942ddc4dfa555dc3e87496092891d8` | Valid / Miles Design LLC |
| MD53100A.sys | `97c93789273ab3d2da624a0917be5a1244fe1cfd678fd5cd8fa3b267732db912` | Valid / Miles Design LLC |

系统检查只核对安装文件哈希和所选 USB 状态，不启动服务或仪器。安装材料在开发缓存 `.tools/installers` 与本机维护缓存保留；恢复仅使用同版本已验证材料，不卸载共享驱动、不删除 TimeLab 或项目用户数据。

当前托管会话使用独立命令/数据临时 TCP 端口，均核验属于本次子进程；客户端仅使用 IPv4 回环地址。不创建防火墙规则。厂商 TSERVE 本身的监听地址策略未改动，不将客户端使用回环地址声称为服务仅监听回环。数据格式、时间戳、分隔符、抽取和单路配置由型号启动参数固定；启动服务仍不开始测量。无需新增 Python 包或 DLL，沿用登记的 TimeLab 1.780A 材料；新增采集功能的带信号验证仍待完成。

随包 `tserve.cpp` 的 `CMD_show_serial` 仅在存在活动采集时读取硬件 Device ID；空闲返回 `Error: No active connection`。`CMD_show_state` 的 Ready 只表示系统存在相同 VID/PID 的 USB，并不核对序列号或占用。`show title` 也需要测量且可修改，不用于身份。

本机解法已实测：依据官方 `timelab.cpp` 的 `USBDEVICE::serial`，对实际读回的 USB 序列号去除冒号或空格后的附加字段。使用微软标准 [USB_DESCRIPTOR_REQUEST](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/usbioctl/ns-usbioctl-_usb_descriptor_request) 和 [USBView 示例](https://github.com/microsoft/Windows-driver-samples/tree/main/usb/usbview)的设备描述符读取方法，只请求已选择父集线器端口；厂家/完整型号/序列号必须全部匹配。普通权限实测成功，无新增 DLL 或 Python 包。身份来源为 USB 描述符；TSERVE 直接硬件身份、固件及采集仍另行判定。

未采用的官方途径：`PSLockDevice` 会遍历设备且调用设置备用接口；`EventAcqSetup` 需要启动采集流程；`selftest` 会执行硬件自检。它们都不符合本轮仅只读身份核验的边界。1.79 beta 发行说明没有列出空闲 TSERVE 身份修复，因此保持已验证的正式版。
