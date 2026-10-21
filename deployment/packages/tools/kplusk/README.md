# K+K FXE 材料恢复

下列材料来自 K+K Messtechnik 官方下载页，供本项目的维护和换电脑操作使用。Git 仅保留本说明及许可 PDF；归档、DLL 和 wrapper 不随 clone 下载。本机旧材料保留在被忽略的原路径。

在“环境与修复 → 准备设备组件”选择 K+K Library 和 Python wrapper，或执行 `python -m lab_control --cli components prepare --component kplusk-python-wrapper`。配置工具另行指定 `--component kplusk-config-tool`。下载到用户维护缓存，按清单安全解压；GUI 不自动启动设备配置/固件更新工具。

- `KK_FirmwareUpload_Win_Ver_5.1.0_25-11-28.zip` 及其中的 `KK_FirmwareUpload_Win32_Ver_5.1.0_25-11-28.exe`：FXE1 Firmware Upload and Configuration Tool。用于通过 USB 读写 FXE 的 IP、子网掩码、网关、CAN ID 和昵称；本项目只引导用户修改网络字段，不自动更新 Firmware 或 Bootloader。
- `KK-Library_Win_Ver_21.2.2_25-11-28.zip`：Windows 64 位 KK Library 及头文件、许可条款。
- `PythonModules_2.1.zip`：官方 Python wrapper 和示例。
- `Terms_of_use.pdf`：随官方软件发布的 K+K 使用条款。

来源：<https://kplusk-messtechnik.de/downloads/index.html>。本目录的材料只在哈希核对通过后使用；不要替换为同名的其他版本。K+K 条款允许将软件集成到自行分发的产品或设备中，但禁止把软件作为独立产品转售或出租；正式发布前仍需由发布负责人确认本项目的分发边界。

源码目录可先执行 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/launch-kplusk-config.ps1 -VerifyOnly` 做离线校验，再去掉 `-VerifyOnly` 打开工具。目录式发行包使用 `maintenance/launch-kplusk-config.ps1`。启动器只做哈希校验和启动，不自动写 IP。

FXE 使用静态 IPv4，不支持依赖 DHCP 的换机流程。新电脑先用 USB 打开配置工具读出完整型号、序列号和原始网络配置，确认实验网口后只修改目标 IP、掩码和网关并点击 `Update Configuration`/`Start Update`；设备重启后拔掉 USB、接交换机，用 KK Library 以 `<IP>[:<Port>]` 直连并读取报告。电脑网卡、交换机和防火墙保持不变，参数读回和官方协议连接分别留档。

`runtime/` 从官方归档的指定成员逐字节恢复；映射、大小与哈希在 [组件清单](../../../components.json)。未签名归档按官方来源与固定哈希验证，不声称 Authenticode 通过。库加载优先复用完整缓存；离线发行按所选组件闭包装配，联网发行不内置这些载荷。
