# K+K FXE 材料恢复

下列材料来自 K+K Messtechnik 官方下载页，供本项目的维护和换电脑操作使用。Git 仅保留本说明及许可 PDF；归档、DLL 和 wrapper 不随 clone 下载。本机旧材料保留在被忽略的原路径。

在“环境与修复 → 准备设备组件”选择 K+K Library 和 Python wrapper，或执行 `python -m lab_control --cli components prepare --component kplusk-python-wrapper`。配置工具另行指定 `--component kplusk-config-tool`。下载到用户维护缓存，按清单安全解压；GUI 不自动启动设备配置/固件更新工具。

- `KK_FirmwareUpload_Win_Ver_5.1.0_25-11-28.zip` 及其中的 `KK_FirmwareUpload_Win32_Ver_5.1.0_25-11-28.exe`：FXE1 Firmware Upload and Configuration Tool。用于通过 USB 读写 FXE 的 IP、子网掩码、网关、CAN ID 和昵称；本项目只引导用户修改网络字段，不自动更新 Firmware 或 Bootloader。
- `KK-Library_Win_Ver_21.2.2_25-11-28.zip`：Windows 64 位 KK Library 及头文件、许可条款。
- `PythonModules_2.1.zip`：官方 Python wrapper 和示例。
- `Terms_of_use.pdf`：随官方软件发布的 K+K 使用条款。

来源：<https://kplusk-messtechnik.de/downloads/index.html>。本目录的材料只在哈希核对通过后使用；不要替换为同名的其他版本。K+K 条款允许将软件集成到自行分发的产品或设备中，但禁止把软件作为独立产品转售或出租；正式发布前仍需由发布负责人确认本项目的分发边界。

源码目录可先执行 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/launch-kplusk-config.ps1 -VerifyOnly` 做离线校验，再去掉 `-VerifyOnly` 打开工具。目录式发行包使用 `maintenance/launch-kplusk-config.ps1`。启动器只做哈希校验和启动，不自动写 IP。

后续现场 K+K 按 DHCP 部署。FXE2 支持 DHCP，使用独立的 FXE2 配置工具；上列 5.1.0 工具和现有启动器只针对旧 FXE1，不得用于 FXE2。FXE2 工具尚未登记为本项目可准备组件，若后续引入须同步组件清单、哈希、许可与下载页面。已启用 DHCP 的设备直接使用有效租约，经官方版本查询核对型号和序列号后关联 MAC；后续扫描重新核验身份并更新地址。旧 FXE1 静态流程不应推广到 FXE2，版本字符串中残留的旧 IP 也不能替代租约地址。

`runtime/` 从官方归档的指定成员逐字节恢复；映射、大小与哈希在 [组件清单](../../../components.json)。未签名归档按官方来源与固定哈希验证，不声称 Authenticode 通过。库加载优先复用完整缓存；离线发行按所选组件闭包装配，联网发行不内置这些载荷。
