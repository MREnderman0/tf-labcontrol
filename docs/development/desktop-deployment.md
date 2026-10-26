# 桌面部署与环境修复

## 交付要求和当前状态

目标为 Windows 10/11 x64。默认联网版包含程序运行所需 Python/Qt，首次运行由用户选择设备组件，自动检查并下载；用户点击“打开安装向导”后由厂商安装器请求所需权限。可另行构建含所选组件及依赖闭包的离线包，最终用户无需寻找资源。设备兼容和物理输出仍按[实机留档规范](../testing/records.md)逐项验收。

已实现组件登记、下载/缓存校验、首次准备页、目录式 EXE、CSV worker、设置内环境检查/修复方案/报告导出。正式应用安装器、自动修复执行器、网络配置应用和跨电脑验收仍待完成。首次准备不配置网络、不切换驱动、不启动设备配置/固件工具、不自动重启。

## 组件维护与缓存

[外部组件清单](../../deployment/components.json)维护用途、型号/接口、版本、架构、系统、来源、大小、SHA256、签名策略、分发依据、检测、安装参数、权限/重启、恢复边界和验证证据。[uv.lock](../../uv.lock)锁定 Python 依赖；型号通过 `deployment_components` 引用组件。未知项保持 `pending`，USB 串口/GPIB 仍须落实准确硬件 ID，不能猜测通用驱动。

`download_sources` 是有序来源，兼容旧 `url`；`archive_members` 保存成员名、目标路径、大小和哈希，`installer_entry` 仅指向已验证安装器，`dependencies` 定义依赖闭包。清单更新后执行 `scripts/generate_download_docs.py`，生成[下载地址与验证状态](../generated/download-resources.md)。

开发、构建、运行、设备接入及维护过程中，凡新增、升级或替换任何依赖或安装材料，包括 Python 包、SDK、DLL、驱动、运行库、服务、工具和归档，必须在同一变更中同步更新下载资源清单、适用的锁文件及下载资源页面；未同步清单的变更不算完成。登记必须包含用途、版本、国内镜像或官方来源、大小、哈希、适用签名、许可和验证状态，未知项标为 `pending`，不得伪报可用。

下载资源页面只由 `scripts/generate_download_docs.py` 根据组件清单、开发材料清单和 `uv.lock` 生成，禁止手工维护重复数据。新增、升级或替换材料后，须运行生成器检查并执行文档链接、锚点和导航检查。

所有新组件下载默认存入 `%LOCALAPPDATA%\TF-LabControl\maintenance\packages`。仍兼容项目 `deployment/packages`、旧 `.tools/installers`、发行目录 `maintenance/packages`、`%ProgramData%\TF-LabControl\maintenance\packages`。旧材料仅在校验后复用；Git 不再跟踪安装包、wheels、工具 EXE、解压 DLL/wrapper。许可、清单、恢复说明保留在 [packages](../../deployment/packages/README.md)。

下载进程禁用代理且只接受 HTTPS；不修改系统代理。厂商优先官方国内站，否则官方直连；先写临时文件，再校验大小、固定 SHA256 和适用的 Authenticode 发布者。网络错误有限重试，4xx 明确失败，校验失败不自动升级或改清单。取消/失败清理临时文件，可再次准备；同一缓存组件使用进程文件锁，结果保存在缓存 `reports`。固定链接被厂商更新时可能不能恢复旧版本，应由维护者核实更新元数据。

K+K 库和 wrapper 从官方 ZIP 中按已确认的映射恢复，与原载荷逐字节一致。未签名归档使用 `official-sha256`，明确显示 NotSigned；不伪报签名通过。配置工具仅准备材料，GUI 不启动它；显式维护入口及静态 IP 流程见[实验网络部署](../guides/lab-network-deployment.md)。

## 开发、准备与构建

唯一开发启动脚本为根目录 `run.bat`。准备脚本复用已有 Python 3.12 x64；缺失时准备已锁定 Python 安装器，显示厂商向导路径，安装后重新运行。完整 wheelhouse 可离线使用；不完整、损坏或 LFS 指针触发按锁文件从清华镜像准备，阿里云只作显式备用，绝不自动回退国外 PyPI。正常安装不重新生成锁文件。开发组默认安装，构建组显式追加。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup-dev.ps1 -Build
# 完整开发缓存，严格离线
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup-dev.ps1 -Build -Offline -Cache D:\LabCache
# 开发材料单独导出，不混入运行包
.venv-dev\Scripts\python.exe scripts/prepare_dev_cache.py --output D:\LabDevCache
.venv-dev\Scripts\python.exe -m lab_control --cli components list
.venv-dev\Scripts\python.exe -m lab_control --cli components prepare --component rs-visa --cache D:\LabCache --report .tools/preparation.json
.venv-dev\Scripts\python.exe -m lab_control --cli components verify --component rs-visa --cache D:\LabCache
.venv-dev\Scripts\python.exe scripts/check_deployment.py --distribution online
.venv-dev\Scripts\python.exe scripts/build_desktop.py --distribution online
# 离线测试包示例；依赖闭包自动包含 K+K Library
.venv-dev\Scripts\python.exe scripts/build_desktop.py --distribution offline --cache D:\LabCache --component rs-visa --component kplusk-python-wrapper
.venv-dev\Scripts\python.exe scripts/check_deployment.py --release --distribution online --report .tools/deployment-release-check.json
```

公共 CLI 的 `prepare --offline` 只复用缓存，`verify` 不联网；安装向导不通过 CLI 启动。原 `scripts/download-vendors.ps1` 是兼容入口，`-VerifyOnly` 只验证。`--cache` 在组件服务中指定输出/验证根目录，在构建/检查中指定材料根目录（可重复）。自定义缓存必须包含清单约定的子目录；不能把临时文件或 LFS 指针当成材料。

构建默认 `online`，输出 `dist/online/TF-LabControl`；离线输出 `dist/offline/TF-LabControl`。已有目录不覆盖，使用 `--output` 指定新目录。联网版不带厂商安装包、wheelhouse、uv 或 Python 安装器。离线版仅装配所选组件及依赖，构建前核验材料；未指定组件代表全部登记组件，存在缺项就报告失败，不悄悄忽略。开发材料通过独立脚本导出。

构建按当前目标环境解析锁文件 marker，核验运行和构建工具的完整适用依赖闭包；包缺失、版本漂移或锁项歧义都会在打包前失败。使用 PyInstaller 已锁定并登记的 packaging 解析工具，不新增运行依赖。`build/<distribution>/deployment/release-manifest.json` 随 EXE 发布，运行包和构建包版本来自实际核验结果，同时记录清单/锁文件哈希、模式及所选组件。维护脚本放 `maintenance`，目录须整体分发，测试包不等于正式安装包。源码 sdist 排除全部评审截图，Git 中的设计基线与评审证据保持不变。

`maintenance-common.ps1` 随维护脚本一起打包，统一处理包含空格/中文的提权参数和硬件互斥。应用及维护入口使用固定 `%LOCALAPPDATA%\TF-LabControl\hardware.lock`，同时兼容当前运行根目录的旧锁；修改 `LAB_CONTROL_HOME` 或输出目录不能绕过互斥。VC++ 安装和选定 53100A 驱动修复全程持锁，不能仅凭进程名称快照判断空闲。记录默认写入运行目录，无需其他电脑存在开发机的 D 盘路径；`-OutputRoot` 可明确覆盖记录位置。

`--release` 保留原有来源、安装、签名策略、分发、目标环境等门禁；联网模式不要求已携带载荷，离线模式额外验证实际材料及解压签名。已有待验收项保持阻断，不因仓库瘦身通过。安装向导可用不等于严格发布通过。

`deployment/labcontrol.spec` 收集 Qt、主题和动态型号模块；不复制开发机厂商驱动。统一启动分发函数先识别 `--csv-worker` / `--deployment-check` / `--cli` 再进入 GUI，这些入口均不显示首次准备页或隐式下载。扫描、连接也不触发下载。

```powershell
.venv-dev\Scripts\python.exe scripts/check_desktop.py --bundle dist/online/TF-LabControl --report .tools/deployment/online-smoke.json
TF-LabControl.exe --deployment-check C:\Temp\labcontrol-smoke.json
TF-LabControl.exe --cli environment --report C:\Temp\environment.json
```

自检复制产物到源码外中文及空格路径，使用隔离数据和模拟界面，禁止 VISA ResourceManager，运行真实 CSV 子进程。它不证明驱动、网络或设备验收。普通用户数据继续位于 `%LOCALAPPDATA%\TF-LabControl` 或 `LAB_CONTROL_HOME`，升级/卸载保留数据。

## 软件设置：环境与修复

入口是“系统设置 → 软件设置 → 环境与修复”；原设备管理“环境诊断”也进入这里。“准备设备组件”与首次页共用公共下载服务。首次页可跳过；已有库存按型号预选，空库存不默认勾选全部，可按设备/接口推荐组件后再调整。点击准备后明确区分材料、已检测组件和设备尚未验收。

材料状态和安装检查结果分别保存。重新检查显示“安装状态待核实”或“未检测到组件”时，已校验的材料路径仍保留；待核实不等于下载或安装失败。检测到兼容版本时禁用重复安装入口；VISA 的只读库候选检查不能替代打开后端或真实设备验收。

安装向导只能由用户点击打开；活动扫描、设备会话或采集期间拒绝启动。打开前重新校验并检测已有兼容版本以避免覆盖/降级；安装期间保留连接和扫描锁，用户完成或关闭厂商向导后点击重新检测。安装失败不标记检测成功。后台检查可取消，保留已完成结果；显示正常、缺失、不兼容、待核实、未需要和检查失败。可查看建议、受影响型号、离线材料、权限/重启要求，并导出 JSON。

公共接口 `inspect_environment`、`plan_repair`、`export_report` 供设置页和 CLI 使用。CLI 为 `lab-control-cli environment [--report 文件] [--repair-plan]`；全局 `--home`、`--config` 放在子命令前。报告版本为 1，检查结果和未执行的修复方案分别保存。报告可能含本机路径和网络信息，仅存运行资料，不提交源码。

检查不打开仪器或发送命令；数据目录检查会创建并清理一个临时写入探针。VISA 检查只查库候选，不创建/关闭可能与活动会话共享的 ResourceManager；SDK DLL 加载、PnP 枚举和本机网络状态不等于整机验收。防火墙和 DHCP 无实际报文验证时显示待核实，未使用组件显示未需要。

当前明确显示“自动修复待安装维护组件接入”。后续执行器须与安装器共用，行为固定为：

1. 首次选择实验网口，后续重新核验本机方案；已有 DHCP 复用，独立实验网自动选择不冲突的地址/通用池并限定服务接口，只启用 DHCP 功能。
2. 检查采集/会话占用，破坏性修复在空闲后执行，期间阻止新连接和任务；不通过关闭输出或重置仪器进行环境修复。
3. 校验受信清单及离线材料，检测已有兼容组件；保存前态、逐项应用、读回验证并记录结果。共享驱动不自动降级，正常组件不强制重装。
4. 独立维护进程正常提权，GUI 普通权限运行。失败撤销本应用拥有且未被外部修改的设置；恢复失败和待重启明确报告，不声称已经恢复。
5. 保留维护缓存，支持原离线安装包补齐；应用无法启动时从安装器维护入口修复。支持离线修复、重启后续装、升级前数据备份与版本迁移；系统重启由用户安排。

网络方案含本机指纹、接口、网段、路由/DNS/网络类型恢复快照和执行顺序，检查换网卡、跨电脑旧方案、默认网关和其他接口/VPN 网段冲突。服务、防火墙的精确前态还必须由未来执行器在写入前采集，旧快照不能直接授权写入。见[实验网络部署](../guides/lab-network-deployment.md)。

本机维护试验暴露的安装门禁已归档在[实验网络部署与换电脑](../guides/lab-network-deployment.md)：UAC 可见提权、物理运行目录、动态网卡选择、UDP 67 防火墙、DHCP-only 服务、租约分阶段验证和失败回滚必须由正式维护执行器完成。开发机的 `hardware-tests/dhcp-*` 只证明该批次的前后态和服务自测，不能复制到另一台电脑，也不能替代干净系统及真实设备验收。

## 换电脑安装检查清单

以下是安装器与“环境与修复”共用执行器的实现要求；当前未接入的自动操作仍为待实现。新用户仅需选择实验网口、处理系统提权提示，并在需要时让设备重新申请地址；不应手工下载依赖、编辑 INI、运行 CMD 或填写防火墙规则。

| 本次暴露或迁移时必须检查的问题 | 安装器自动处理与通过依据 |
| --- | --- |
| 仓库大文件仍是 Git LFS 指针、缓存缺失或损坏 | 按离线清单核对文件大小和 SHA256；开发准备或联网首次准备阶段恢复实际材料；离线包必须携带所选组件及依赖闭包；缺项明确列出，不把指针当安装程序 |
| 新电脑没有 Python、uv 或构建环境 | 正式安装包运行冻结程序，不要求用户安装开发环境；源码安装先检测解释器版本/位数，再使用缓存安装；依赖与锁文件一致 |
| 下载受代理、国外源或临时链接影响 | 优先完整离线缓存；开发补齐按国内镜像/官方直连策略执行并保存来源、版本、签名和哈希；不修改系统代理 |
| TimeLab 安装成功但 USB 设备仍缺驱动 | 应用安装、驱动暂存和实际绑定分开检测；按准确硬件 ID 核对驱动、设备错误码和重启需求，必要时从外部 PowerShell 启动 `scripts/repair_53100a_driver.ps1`；详见 [53100A 部署维护源](../../src/lab_control/instruments/devices/microchip_53100a/deployment.md)；退出码 0 不能替代设备检查 |
| 厂商安装和 PnPUtil 都返回 0，但设备报错 39 | 保存设备 ProblemStatus、SetupAPI 日志和对应 CodeIntegrity 事件的策略 GUID/名称；区分普通文件签名有效、驱动暂存成功和内核加载通过。命中 Windows Driver Policy 时寻找 WHCP 签名版本；同哈希新版安装包内驱动不重复安装，维护器不自动关闭安全策略 |
| 已装 VISA/SDK 版本、位数或路径与开发机不同 | 检测现有兼容运行库并复用，逐项核对 x64、版本、DLL 及必要导出；不复制开发机绝对路径、不降级共享驱动、不自动切换 USB 驱动 |
| VC++ 运行库已安装但安装器反复覆盖旧版本 | 读取 x64 运行库注册表版本；版本不低于清单要求时跳过安装，较高版本也不降级。需要补齐时由 `scripts/ensure_vc_runtime.ps1` 外部 PowerShell 提权执行并做安装后读回 |
| 构建电脑 PATH 混入其他软件的 DLL | 使用受控构建环境，目录产物在源码外和中文/空格路径验证；安装成功与冻结 worker 可运行分别留证 |
| 网络服务已运行但仪器仍不可见 | 执行[网络部署门禁](../guides/lab-network-deployment.md#换电脑实测踩坑与安装器门禁)，分别报告服务、租约 ACK、发现、身份核验；用户取消提权、设备离线及未续租均保留准确状态 |
| 二次安装、升级、失败或卸载影响已有资料 | 检测并复用正常组件，维护前保存快照，重复修复不重复建规则；只回滚本应用拥有且未被外部修改的设置，保留库存、历史、离线缓存与待重启状态 |

上述结果须写入本机维护报告，并在干净 Windows 10/11 x64、离线、普通用户、拒绝提权、已有共享驱动及重复修复场景验收。尚未完成的检测、自动配置和目标电脑测试继续阻断正式发布，不用本机成功代替跨电脑通过。

## 验收与发布

原部署阶段证据见[历史部署验收记录](../reviews/desktop-deployment-review.md)；此次瘦身证据见[仓库瘦身验收](../reviews/repository-slimming-review.md)。

开发回归覆盖缺失/损坏材料、架构、未登记依赖、源码及冻结 worker、中文路径、转换失败保留数据、设置页异步/取消/失败/导出、网络方案冲突和旧快照。文档执行生成一致性及链接导航检查。

正式发布须在干净 Windows 10/11 x64 上验证离线安装、普通权限启动、已装不同 VISA 的电脑、无驱动/损坏缓存、重复修复、拒绝提权、需重启、升级/卸载、低磁盘空间，以及各实际接口和型号。按设备分别留档；发布签名、许可材料、安装器恢复、目标电脑和实机验收缺一项都保留待完成状态。

设计依据：[PyInstaller 运行模式](https://pyinstaller.org/en/stable/operating-mode.html)、[冻结进程与子进程](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html)、[Qt Windows 支持](https://doc.qt.io/qt-6/supported-platforms.html)。版本升级须重新确认 Windows 10 及厂商驱动兼容性。
