# 桌面部署与环境修复

## 交付要求和当前状态

目标为 Windows 10/11 x64。最终用户通过离线安装包获得所需运行依赖和设备驱动，首次选择实验网口后由维护组件自动配置实验网络。设备兼容和物理输出仍按[实机留档规范](../testing/records.md)逐项验收。

当前已提供统一组件清单、开发/发布检查、目录式 EXE 构建、独立 CSV worker、设置内环境检查/修复方案/报告导出。自动安装/修复执行器、正式安装向导、网络配置应用和跨电脑验收尚未交付。“方案生成”不表示已经修复。当前网络入口仍只读。

## 组件维护

[外部组件清单](../../deployment/components.json)是来源；[uv.lock](../../uv.lock)锁定 Python 及构建依赖。新增运行依赖必须同时登记，型号 `DeviceDefinition.deployment_components` 引用组件 ID。公共层处理部署，型号协议仍留在型号包。

外部组件须记录用途、适用型号/接口、版本/架构/系统、官方来源、安装包名、SHA256、签名发布者、分发依据、检测方法、安装参数/顺序、退出码、权限/重启、恢复边界和验证证据。无法确定时填 `null` / `pending`，不能猜测静默参数、通用驱动或许可。USB 串口和 GPIB 组件必须落实到实际接口硬件 ID；不拿一种适配器驱动代表所有适配器。

已有 VISA、PicoSDK、Thorlabs 缓存哈希及来源已登记；安装/分发/目标电脑验证尚有待补项。TSERVE、DHCP、串口/GPIB 和 VC++ 组件也在台账中。待补项由开发/发布负责人解决，不交给最终用户搜索资源。

`scripts/download-vendors.ps1` 读取清单，复用缓存并核对锁定哈希和发布者；`-VerifyOnly` 禁止下载。开发下载遵循[环境规范](environment.md)，从国内镜像或官方直连获取。不安装驱动、不改变系统设置。

离线材料开发时位于 `.tools/installers`；正式部署保存在 `%ProgramData%\TF-LabControl\maintenance\packages`，也可从发行目录的 `maintenance/packages` 读取。开发机绝对地址、网卡、租约和真实资产资料不进入清单。

## 构建与检查

在已有下载策略环境中同步固定的构建工具：

```powershell
$env:UV_PROJECT_ENVIRONMENT='.venv-dev'
$env:HTTP_PROXY=''; $env:HTTPS_PROXY=''; $env:ALL_PROXY=''; $env:NO_PROXY='*'
.venv-dev\Scripts\uv.exe sync --locked --group build --python .venv-dev\Scripts\python.exe --no-python-downloads --default-index https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple/
.venv-dev\Scripts\python.exe scripts/check_deployment.py
.venv-dev\Scripts\python.exe scripts/check_deployment.py --release --report .tools/deployment-release-check.json
.venv-dev\Scripts\python.exe scripts/build_desktop.py
```

上述环境变量仅在本次 PowerShell 进程有效。开发检查拒绝未登记依赖及失效型号组件引用，并列出待补项；严格发布检查还校验材料、安装/分发证据等，当前未齐备时应返回非零退出码。ZIP 内安装文件签名还需后续发布暂存校验；当前严格检查对此保持阻断。

构建检查安装版本是否符合锁文件，生成 `build/deployment/release-manifest.json`，包含运行依赖闭包、构建版本和清单哈希，随 EXE 打包。测试版输出 `dist/TF-LabControl/TF-LabControl.exe`，目录必须整体分发；它不是已完成的正式安装包。`build_desktop.py --release` 在严格检查通过后才允许继续构建，完整发行还需安装器、许可和目标电脑验收门禁。

`deployment/labcontrol.spec` 收集 Qt 插件、主题、动态型号模块和运行元数据；不从开发机复制厂商驱动 DLL。唯一启动分发函数先识别 `--csv-worker` / `--deployment-check` / `--cli`，再进入 GUI。源码入口和 EXE 共用该路径，CSV worker 不创建 GUI 或硬件会话。

```powershell
# 将整个产物复制到源码目录以外后执行；报告位置也用于 PNG 截图
TF-LabControl.exe --deployment-check C:\Temp\labcontrol-smoke.json
TF-LabControl.exe --cli environment --report C:\Temp\environment.json
```

部署自检使用临时隔离数据和模拟界面，禁止 VISA ResourceManager，验证型号、主题、版本、记录和真实子进程 CSV 转换。它不证明驱动安装、真实网络或设备验收通过。普通用户数据继续位于 `%LOCALAPPDATA%\TF-LabControl` 或显式 `LAB_CONTROL_HOME`；升级前使用一致性备份，卸载默认保留数据。

## 软件设置：环境与修复

入口是“系统设置 → 软件设置 → 环境与修复”；原设备管理“环境诊断”也进入这里。后台检查可取消，保留已完成结果；显示正常、缺失、不兼容、待核实、未需要和检查失败。可查看建议、受影响型号、离线材料、权限/重启要求，并导出 JSON。

公共接口 `inspect_environment`、`plan_repair`、`export_report` 供设置页和 CLI 使用。CLI 为 `lab-control-cli environment [--report 文件] [--repair-plan]`；全局 `--home`、`--config` 放在子命令前。报告版本为 1，检查结果和未执行的修复方案分别保存。报告可能含本机路径和网络信息，仅存运行资料，不提交源码。

检查不打开仪器或发送命令；数据目录检查会创建并清理一个临时写入探针。VISA 检查只查库候选，不创建/关闭可能与活动会话共享的 ResourceManager；SDK DLL 加载、PnP 枚举和本机网络状态不等于整机验收。防火墙和 DHCP 无实际报文验证时显示待核实，未使用组件显示未需要。

当前明确显示“自动修复待安装维护组件接入”。后续执行器须与安装器共用，行为固定为：

1. 首次选择实验网口，后续重新核验本机方案；已有 DHCP 复用，独立实验网自动选择不冲突的地址/通用池并限定服务接口，只启用 DHCP 功能。
2. 检查采集/会话占用，破坏性修复在空闲后执行，期间阻止新连接和任务；不通过关闭输出或重置仪器进行环境修复。
3. 校验受信清单及离线材料，检测已有兼容组件；保存前态、逐项应用、读回验证并记录结果。共享驱动不自动降级，正常组件不强制重装。
4. 独立维护进程正常提权，GUI 普通权限运行。失败撤销本应用拥有且未被外部修改的设置；恢复失败和待重启明确报告，不声称已经恢复。
5. 保留维护缓存，支持原离线安装包补齐；应用无法启动时从安装器维护入口修复。支持离线修复、重启后续装、升级前数据备份与版本迁移；系统重启由用户安排。

网络方案含本机指纹、接口、网段、路由/DNS/网络类型恢复快照和执行顺序，检查换网卡、跨电脑旧方案、默认网关和其他接口/VPN 网段冲突。服务、防火墙的精确前态还必须由未来执行器在写入前采集，旧快照不能直接授权写入。见[实验网络部署](../guides/lab-network-deployment.md)。

## 验收与发布

本轮实际构建、回归、界面和待验收边界见[验收记录](../reviews/desktop-deployment-review.md)。

开发回归覆盖缺失/损坏材料、架构、未登记依赖、源码及冻结 worker、中文路径、转换失败保留数据、设置页异步/取消/失败/导出、网络方案冲突和旧快照。文档执行生成一致性及链接导航检查。

正式发布须在干净 Windows 10/11 x64 上验证离线安装、普通权限启动、已装不同 VISA 的电脑、无驱动/损坏缓存、重复修复、拒绝提权、需重启、升级/卸载、低磁盘空间，以及各实际接口和型号。按设备分别留档；发布签名、许可材料、安装器恢复、目标电脑和实机验收缺一项都保留待完成状态。

设计依据：[PyInstaller 运行模式](https://pyinstaller.org/en/stable/operating-mode.html)、[冻结进程与子进程](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html)、[Qt Windows 支持](https://doc.qt.io/qt-6/supported-platforms.html)。版本升级须重新确认 Windows 10 及厂商驱动兼容性。
