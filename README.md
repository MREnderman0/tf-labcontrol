# TF LabControl

PySide6 实验室设备软件，包含真实通信兼容层、设备扫描总览、分类控制页、实时监测及 CLI 实测工具。当前版本 `0.3.0` 已接通共享真实/Mock 页面；部分资产已有通信及控制证据，物理性能按具体测试项目验收。

## 安装和启动

需要 Windows x64 和 Python 3.12 x64。在项目目录执行（无需全局 uv）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup-dev.ps1
.venv-dev\Scripts\lab-control.exe
```

日常启动只需双击根目录 `run.bat`，使用项目 `.venv-dev`。`python -m lab_control` 和 `lab-control` 调用同一正式启动函数，持续开发同一套软件。未安装 VISA/SDK 时仍可打开，环境诊断会显示缺失项；空库存不会填入模拟设备。厂商驱动与 DLL 的安装见[实机指南](docs/testing/hardware-testing.md)。

**项目下载约定：优先国内镜像，默认禁用安装进程代理，不消耗 VPN 流量，不自动回退国外 PyPI。** 默认清华、可显式指定阿里云；详见[开发环境与下载策略](docs/development/environment.md)。支持 VISA 的设备优先通过 PyVISA 接入；同一设备多地址须核实身份，避免重复连接。

```powershell
.venv-dev\Scripts\lab-control.exe --config config/examples/visa.toml
.venv-dev\Scripts\python.exe -m lab_control
.venv-dev\Scripts\lab-control-cli.exe models
.venv-dev\Scripts\lab-control-cli.exe doctor
```

`visa.toml` 中的地址为示例，使用前复制到自己的配置目录并替换。`run.bat --config "配置文件路径"` 同样支持参数透传。Mock 仅用于内部测试和开发预览，共用正式界面组件；CLI 的 `--mock` 使用隔离模拟库存。

日常双击启动会读取运行目录的 `config/devices.toml`（默认 `%LOCALAPPDATA%\TF-LabControl\config\devices.toml`，也遵循 `LAB_CONTROL_HOME`）。将需要扫描的已知网口设备明确写入此文件；VISA 自动枚举未列出网络设备时，配置地址仍参与扫描。显式 `--config` 覆盖默认文件，不合并目录中的备份或其他配置；没有默认文件时继续只做正常枚举，配置错误会明确报错。扫描不会遍历整个网段。

交换机上连接多台设备时，可通过运行目录 `config/lan-discovery.toml` 启用 [LAN 自动发现与地址更新](docs/architecture/lan-discovery.md)。软件用已核验的厂家、完整型号和序列号保留资产身份，IP 变化不丢失名称、槽位、实验限值和历史；显式固定地址保持固定。发现范围限于指定实验网卡/网段，不自动修改仪器网络参数。

前端固定为用户确认的[历史深色琥珀分类卡片版](direction-approved.md)：左侧导航、顶部分类、三列分类卡片，点击卡片展开该类设备池。正式界面与内部预览共用唯一主题。数值输入下方显示范围和来源，在失焦、回车或应用时自动修正到最近边界，修正不发送设备命令。仪器读回保留原值；后端拒绝超界请求。SMA100B 连接后读取实机可设范围，RF 输出限幅单列显示，实验限制可进一步收紧。详见[输入范围规则](docs/guides/input-ranges.md)。

信号源在设备管理调节，电源、示波器、频谱及测量类参数放在实时监测。设备与监测总览卡片随窗口排版，电子学/光学分别保存布局。普通监测不写实验数据，显式开始记录后逐帧保存；相噪仪定时采集在开始后自动保存原始相位、相噪及最终 ADEV/MDEV。完整操作见[分类控制与监测指南](docs/guides/control-and-monitoring.md)。

文档按任务导航见[文档总入口](docs/README.md)；后续设备统一按[新设备接入流程](docs/testing/onboarding.md)执行，历史实测通过[离线台账](docs/testing/records.md)查询。

## 首次接设备

1. 在厂家软件中确认驱动、完整型号、序列号及资源地址，关闭占用仪器的厂家连接。
2. 打开真实总览，扫描设备或添加已确认资源；检查类别、身份和适配器，再点击连接。
3. 用 `lab-control-cli --config <配置> --resource <完整资源> test` 生成只读身份/能力/状态报告。运行真实 CLI 前退出本软件真实 GUI，两者使用进程互斥。
4. 根据[型号卡片](docs/device-research-index.md)选择一项设置和一次采集，按[实机测试指南](docs/testing/hardware-testing.md)核对物理结果。

输出、触发、归零等操作需要显式确认；普通参数也可能影响已经打开的输出，应先核对接线与负载。断开软件连接不会关闭物理输出，重连不会恢复预设、列表或输出。

## 文件位置

| 内容 | 位置 |
| --- | --- |
| 领域模型 / 传输 / 型号适配器 | `src/lab_control/domain`、`transport`、`instruments` |
| 扫描 / 会话 / 存储 | `discovery`、`application`、`storage` |
| 共用窗口 / 分类控制 / 监测页面 | `ui/real_window.py`、`ui/pages/device_control/`、`ui/pages/monitoring/` |
| 监测调度 / 展示元数据 / 隔离演示 | `application/monitoring.py`、`application/controls.py`、`demo/workspace.py` |
| CLI / 旧设计壳子（仅历史回归） | `cli/main.py`、`ui/main_window.py` |
| 使用示例与配置模板 | [examples](examples/README.md)、[config/examples](config/examples/README.md) |
| 实际注册能力 | [能力矩阵](docs/generated/capability-matrix.md)与 [JSON](docs/generated/capability-matrix.json) |
| 文档入口 | [docs/README.md](docs/README.md) |

库存 SQLite、日志、报告和采集文件默认放在 `%LOCALAPPDATA%\TF-LabControl`；通过 `LAB_CONTROL_HOME` 可改运行目录。设置中的“数据保存一级目录”只影响新采集数据，按单设备采集、日期、开始时间与设备身份分层；电子学/光学保存在元数据中，历史 `data/electronic`、`data/optical` 保留原样。模拟数据另存 `simulation`。详细结构见[数据模型](docs/data/data-model.md)。SDK DLL、临时下载和实测文件不进入源码目录。

## 开发检查

真实设备测试必须遵循[实机测试记录规范](docs/testing/records.md)：每台、每次均留档，失败和受阻也记录；更新运行目录 `hardware-tests/index.md`，分别记录连接、参数控制、恢复和物理输出验收。

```powershell
uv run ruff check src tests scripts examples
uv run ruff format --check src tests scripts examples
uv run mypy
uv run pytest -q
uv run python scripts/generate_device_docs.py --check
uv run python scripts/check_docs.py
uv build
```

修改能力后运行 `uv run python scripts/generate_device_docs.py`，同步生成矩阵和型号卡片。硬件测试默认跳过，启用方法见实机指南。所有测试使用独立临时目录；厂商运行库不随 wheel 分发。

当前扩展边界见[能力矩阵的未实现项](docs/generated/capability-matrix.md#未实现--待补项)：复杂序列、完整分段采集、无缝无限流和部分选件应用尚未开放，未声明的功能返回 `unsupported`。

## 桌面部署与环境检查

系统设置中的“环境与修复”提供后台检查、修复方案、所选组件下载和报告导出。首次 GUI 运行会显示可跳过的准备页；材料校验完成后，可由用户点击打开厂商安装向导。网络自动配置仍待实现。

Git 只保存开发材料及资源清单，第三方安装包、wheels 和解压运行库使用本机缓存。默认联网版 EXE 自带 Python/Qt，按需准备厂商组件；离线版从验证过的缓存单独构建。参见[部署规范](docs/development/desktop-deployment.md)、[下载资源](docs/generated/download-resources.md)和[缓存恢复说明](deployment/packages/README.md)。
