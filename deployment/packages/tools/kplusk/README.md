# K+K FXE 离线软件包

这些文件来自 K+K Messtechnik 官方下载页，供本项目的维护材料和换电脑操作使用：

- `KK_FirmwareUpload_Win_Ver_5.1.0_25-11-28.zip` 及其中的 `KK_FirmwareUpload_Win32_Ver_5.1.0_25-11-28.exe`：FXE1 Firmware Upload and Configuration Tool。用于通过 USB 读写 FXE 的 IP、子网掩码、网关、CAN ID 和昵称；本项目只引导用户修改网络字段，不自动更新 Firmware 或 Bootloader。
- `KK-Library_Win_Ver_21.2.2_25-11-28.zip`：Windows 64 位 KK Library 及头文件、许可条款。
- `PythonModules_2.1.zip`：官方 Python wrapper 和示例。
- `Terms_of_use.pdf`：随官方软件发布的 K+K 使用条款。

来源：<https://kplusk-messtechnik.de/downloads/index.html>。本目录的材料只在哈希核对通过后使用；不要替换为同名的其他版本。K+K 条款允许将软件集成到自行分发的产品或设备中，但禁止把软件作为独立产品转售或出租；正式发布前仍需由发布负责人确认本项目的分发边界。

源码目录可先执行 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/launch-kplusk-config.ps1 -VerifyOnly` 做离线校验，再去掉 `-VerifyOnly` 打开工具。目录式发行包使用 `maintenance/launch-kplusk-config.ps1`。启动器只做哈希校验和启动，不自动写 IP。

FXE 使用静态 IPv4，不支持依赖 DHCP 的换机流程。新电脑先用 USB 打开配置工具读出完整型号、序列号和原始网络配置，确认实验网口后只修改目标 IP、掩码和网关并点击 `Update Configuration`/`Start Update`；设备重启后拔掉 USB、接交换机，用 KK Library 以 `<IP>[:<Port>]` 直连并读取报告。电脑网卡、交换机和防火墙保持不变，参数读回和官方协议连接分别留档。

`runtime/` is the extracted, hash-verified DLL/wrapper payload used by the project compatibility adapter and directory build; it is not a different vendor version.
