# TF LabControl

PySide6 实验室设备软件，包含真实通信兼容层、设备扫描总览、分类控制页、实时监测及 CLI 实测工具。当前版本 `0.3.0` 已接通共享真实/Mock 页面；真实设备验收由现场接线后完成。

## 安装和启动

需要 Windows x64、Python 3.12 和 uv。在项目目录执行：

```powershell
$env:UV_PROJECT_ENVIRONMENT = ".venv-dev"
uv sync --locked
uv run lab-control
```

使用独立开发环境，避免覆盖正在运行的旧演示程序环境。也可以双击 `run_real.bat`。真实入口未安装 VISA/SDK 时仍可打开，环境诊断会显示缺失项；扫描不会加入演示设备。厂商驱动与 DLL 的安装见[实机指南](docs/hardware-testing.md)。

```powershell
uv run lab-control --config config/examples/visa.toml
uv run lab-control-demo
uv run lab-control-cli models
uv run lab-control-cli doctor
```

`visa.toml` 中的地址为示例，使用前复制到自己的配置目录并替换。演示入口 `lab-control-demo` 不访问仪器；CLI 的 `--mock` 使用隔离模拟库存。

双击 `run_demo.bat` 可查看 12 类、24 台 A/B 模拟设备。信号源/电源在设备管理调节，示波器/频谱及测量类参数放在实时监测。总览卡片自动排版，电子学/光学分别保存布局；仅监测不写实验数据，显式开始记录后才保存原始批次。完整操作见[分类控制与监测指南](docs/control-and-monitoring.md)。

## 首次接设备

1. 在厂家软件中确认驱动、完整型号、序列号及资源地址，关闭占用仪器的厂家连接。
2. 打开真实总览，扫描设备或添加已确认资源；检查类别、身份和适配器，再点击连接。
3. 用 `lab-control-cli --config <配置> --resource <完整资源> test` 生成只读身份/能力/状态报告。运行真实 CLI 前退出本软件真实 GUI，两者使用进程互斥。
4. 根据[型号卡片](docs/device-research-index.md)选择一项设置和一次采集，按[实机测试指南](docs/hardware-testing.md)核对物理结果。

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

库存 SQLite、日志、报告和采集文件默认放在 `%LOCALAPPDATA%\TF-LabControl`；通过 `LAB_CONTROL_HOME` 可改数据根目录。数据按 `data/electronic`、`data/optical` 分开。模拟数据另存 `simulation`。SDK DLL、临时下载和实测文件不进入源码目录。

## 开发检查

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
