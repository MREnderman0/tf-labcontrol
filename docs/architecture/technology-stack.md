# Python 技术栈决策

## 0.3.0 实际采用状态

当前采用 Python 3.12 x64、PySide6、PyQtGraph、PyVISA、PySerial、NumPy；使用内置 sqlite3、JSONL/CSV/NPZ、ThreadPoolExecutor 与 Qt Signal。Agent 接入使用官方 MCP Python SDK 2.3.0、asyncio 和 cryptography，传输依赖随 SDK 锁定。setuptools 生成 wheel/sdist，PyInstaller 生成 Windows 目录式 EXE。依赖和版本以根目录 pyproject.toml/uv.lock 为准。图形默认软件渲染，显示抽稀不影响原始数据导出。

SciPy、h5py、structlog 以及独立 32 位 SDK worker 仍为后续规划，不属于当前运行依赖。目录式构建已实现，正式应用安装器与跨电脑验收仍未完成。完整运行架构见 [system-architecture.md](system-architecture.md)。

## 1. 结论

本项目采用 Python 3.12 x64 和 PySide6，支持范围由 `requires-python = ">=3.12,<3.13"` 约束。应用是 Windows 桌面端的模块化单体：Qt 负责窗口、导航、表单和绘图容器，应用层负责设备会话、功能槽位、采集编排、监测规则和日志，传输层负责 VISA、串口、TCP 以及厂商 SDK。

外部 Agent 可通过 [MCP 协作接口](../guides/agent-collaboration.md)编排获授权的操作；内置实验流程编辑器仍未实现。单进程管理 6–20 台真实设备；模拟版提供 24 台示例。每设备一个串行 worker，SDK 位数不匹配明确报错；独立 SDK 进程隔离为后续工作。

## 2. 组件与后续规划

| 子系统 | 方案 | 采用结论 | 许可证/边界 |
| --- | --- | --- | --- |
| 运行时 | Python 3.12 x64 | 直接采用，暂不支持 3.13+ | Python Software Foundation License |
| UI | [PySide6 + Qt Widgets](https://doc.qt.io/qtforpython-6/) | 直接采用 | Qt 社区版 LGPLv3/GPL 或商业许可；发布前做许可证复核 |
| UI 样式 | 自定义 QSS；Qt Designer 可选 | 直接采用 | 项目自有样式，避免绑定 GPL 主题组件 |
| VISA | [PyVISA](https://pyvisa.readthedocs.io/en/latest/) + NI-VISA | 直接采用 | PyVISA MIT；NI-VISA 和厂商驱动属于目标机前置条件 |
| 纯 Python VISA 后端 | [pyvisa-py](https://github.com/pyvisa/pyvisa-py) | 可选后端 | MIT；不能替代已验证的 NI-VISA 部署 |
| 非 VISA 串口 | [PySerial](https://github.com/pyserial/pyserial) | 直接采用 | BSD-3-Clause |
| 实时绘图 | [pyqtgraph](https://pyqtgraph.com/) | 直接采用 | MIT；波形、频谱和监测趋势统一使用 |
| 数值计算 | [NumPy](https://numpy.org/) + [SciPy](https://scipy.org/) | NumPy 已采用；SciPy 待处理工作台 | BSD-3-Clause |
| 元数据 | 内置 `sqlite3` | 直接采用 | Python 标准库；设备、预设、任务和索引 |
| 科学数据 | [h5py/HDF5](https://www.h5py.org/) | 后续长时记录规划 | h5py BSD-3-Clause |
| 原始批次数组 | NumPy NPZ + JSON 快照 | 已采用 | NumPy 许可；与 CSV 一同导出 |
| 导出 | CSV | 直接采用 | 标准文本格式，供 Excel、MATLAB、Python 使用 |
| 诊断日志 | JSONL 审计；[structlog](https://www.structlog.org/) 待扩展 | JSONL 已采用 | structlog 不属于当前依赖 |
| 并发 | ThreadPoolExecutor + 调度线程 + Qt Signal/快照 | 已采用 | 设备 I/O 禁止在主线程执行 |
| 依赖管理 | [uv](https://docs.astral.sh/uv/) + `pyproject.toml` + `uv.lock` | 已采用 | uv Apache-2.0 |
| Windows 打包 | setuptools wheel/sdist；[PyInstaller](https://pyinstaller.org/) one-folder | 已实现目录式构建及冻结 worker 检查 | SDK/驱动独立准备，正式安装器待实现 |
| 测试 | [pytest](https://docs.pytest.org/)、[pytest-qt](https://pytest-qt.readthedocs.io/) + Mock Transport | 直接采用 | pytest/pytest-qt MIT |

## 3. 取舍和边界

- 不以 PyQt6 为首选，避免 GPL/商业许可决策阻塞；如果后续有明确商业授权，可重新评估。
- 不直接采用 [PyQt-Fluent-Widgets](https://github.com/zhiyiYo/PyQt-Fluent-Widgets)，其 GPLv3 许可不适合作为内部工具的默认耦合项。需要类似视觉时，用自定义 QSS、Qt Widgets 和少量自绘控件实现。
- 第一版不采用 QML。团队只维护 Python 和 Qt Widgets 一套界面技术，减少 Python 与 QML 的边界调试成本。
- 不把 [qasync](https://github.com/CabbageDevelopment/qasync) 作为核心并发模型。仪器命令大多是阻塞式 VISA/串口调用，使用 Qt worker 更容易保证每台设备命令串行；后续确有 asyncio 网络设备时再评估 qasync。
- 单帧导出保留 NPZ/JSON/CSV；连续记录使用版本化 SQLite/BLOB 分段保存原始帧，再由独立子进程转换分析 CSV。HDF5 尚未引入，格式与兼容规则见[数据模型](../data/data-model.md)。
- 第一版不强制引入 Parquet。若后续出现大规模表格分析和跨工具查询需求，再增加 Parquet 导出或分析副本。
- 图表只保留 pyqtgraph 一套主方案，避免同时维护多套交互语义。

## 4. 运行与部署前置条件

源码开发需要 Python 3.12 x64 和项目环境；目录式 EXE 自带 Python/Qt。目标机器按所选设备准备经过验证的 VISA 实现、USB/LAN/GPIB/串口驱动及厂商 SDK/DLL。Thorlabs PM100D 优先走官方 DLL/SDK，不预设通用 SCPI。

当前可以构建 wheel/sdist 和 PyInstaller one-folder；源码外入口、QSS 与冻结 worker 有独立检查。联网版按需准备厂商材料，离线版显式装配所选组件及依赖闭包。现行构建步骤和待验收边界以[桌面部署](../development/desktop-deployment.md)为准，不将开发机构建成功等同正式交付。

## 5. 版本策略

当前依赖已在 `pyproject.toml` 声明并由 `uv.lock` 固定；新增依赖先检查是否实际使用。升级前用协议夹具、分类页面、20 个并发会话和已注册适配器回归。

## 6. 参考资料

- [Qt for Python 文档](https://doc.qt.io/qtforpython-6/)
- [Qt 许可说明](https://www.qt.io/licensing/)
- [PyVISA 通信文档](https://pyvisa.readthedocs.io/en/latest/introduction/communication.html)
- [NI-VISA](https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html)
- [h5py 文档](https://docs.h5py.org/en/stable/)
- [PyInstaller 文档](https://pyinstaller.org/en/stable/)
