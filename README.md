# TF LabControl

Windows 实验室设备控制与监测软件，使用 PySide6、独立型号兼容模块及真实通信服务。界面沿用已确认的[深色琥珀分类卡片基线](direction-approved.md)；软件验证与真实设备验收分别记录。

## 安装和启动

- **源码环境：** Windows 10/11 x64、Python 3.12 x64；唯一开发启动脚本为根目录 `run.bat`，使用项目 `.venv-dev`。环境准备和下载策略见[开发环境](docs/development/environment.md)。
- **目录式测试包：** 保留完整目录，运行其中的 `TF-LabControl.exe`，无需另外安装 Python/Qt。正式应用安装器与交付验收仍待完成，现有构建和组件准备见[桌面部署](docs/development/desktop-deployment.md)。

## 快速开始

首次使用源码时，在项目目录准备环境，然后启动：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup-dev.ps1
./run.bat
```

新电脑首次准备后，先完成[依赖导入与完整性检查](docs/development/environment.md#首次部署的依赖检查)，再连接设备。后续直接双击 `run.bat`。选择设备、配置资源和开始监测见[使用指南](docs/guides/control-and-monitoring.md#启动与设备选择)；设备协议和范围以[型号维护源](docs/devices/README.md)为准。

## 文档导航

完整导航见[项目文档](docs/README.md)。旧章节锚点保留在下表，操作步骤在对应维护源更新。

| 任务 | 入口 |
| --- | --- |
| <a id="首次接设备"></a>首次接设备与实机测试 | [连接与测试](docs/testing/hardware-testing.md)、[逐台留档](docs/testing/records.md) |
| 控制、监测与保存 | [使用指南](docs/guides/control-and-monitoring.md)、[输入范围](docs/guides/input-ranges.md) |
| Agent 协作实验 | [MCP 接入、授权与 Codex 配置](docs/guides/agent-collaboration.md) |
| <a id="文件位置"></a>源码结构与运行文件 | [系统架构](docs/architecture/system-architecture.md#目录职责)、[数据模型](docs/data/data-model.md#实际存储结构) |
| <a id="开发检查"></a>开发检查与构建验证 | [软件验证](docs/development/software-validation.md)、[文档维护](docs/development/documentation.md) |
| <a id="桌面部署与环境检查"></a>桌面部署与环境检查 | [部署规范](docs/development/desktop-deployment.md)、[下载资源](docs/generated/download-resources.md) |
| 接入新型号 | [接入流程](docs/testing/onboarding.md)、[设备模块规范](docs/architecture/device-modules.md) |
| 历史证据与实现边界 | [评审记录](docs/reviews/README.md)、[能力矩阵](docs/generated/capability-matrix.md)、[需求边界](docs/requirements/overview.md) |
