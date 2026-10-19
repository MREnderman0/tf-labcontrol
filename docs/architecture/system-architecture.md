# 系统架构与工程边界（0.3.0）

当前交付为 Python 模块化单体：真实通信、设备总览、分类控制和实时监测已接通；真实/演示入口共用页面并注入隔离服务。自动实验、阈值告警和数据处理工作台仍在后续范围。

## 目录职责

| 路径（src/lab_control 下） | 职责 |
| --- | --- |
| domain/instruments.py | Qt 无关的身份、能力、参数、命令、结果和测量 |
| domain/models.py | 原演示界面的展示模型 |
| transport/ | VISA/串口/TCP/VICP 分帧及 SDK 加载 |
| instruments/ | 基础适配器、独立型号 Profile、SDK 类和数据解析 |
| discovery/ | provider 枚举、有限探测、库存合并、取消与扫描记录 |
| application/ | Runtime 生命周期、每设备 worker、租约、安全校验和事件 |
| storage/ | SQLite 库存/扫描、JSONL 日志、CSV/NPZ/Touchstone/报告 |
| cli/ | 环境诊断、扫描、只读测试、设置和采集 |
| ui/real_window.py、ui/controller.py | 共用应用窗口、总览、Qt 快照/Future 桥接 |
| ui/pages/device_control/ | 能力表单、单位、草稿、数组表格/预览、确认与结果 |
| ui/pages/monitoring/ | 类型页、PyQtGraph、响应式卡片总览 |
| application/monitoring.py、controls.py | Qt 无关的调度/记录、中文标签/页面分组/数据源元数据 |
| demo/workspace.py、demo/instrument.py | 24 台 A/B Mock 和参数相关模拟数据 |
| ui/main_window.py | 旧设计壳子，仅保留历史回归，不再作为启动入口 |

配置模板在 config/examples；用户使用示例在 examples；生成器在 scripts；测试分别放 unit/integration/fixtures/hardware。厂商 DLL、临时下载和现场数据均在源码外。

## 运行入口

run.bat、Python 模块入口和 lab-control 统一调用 app.main，创建真实 ApplicationRuntime 和 RealWindow；内部测试和预览注入隔离 Mock ApplicationRuntime 并显示同一窗口；lab-control-cli 调用同一会话服务。配置文件只描述资源/适配器/驱动，不允许混入厂家命令。

真实进程使用每用户固定位置的独占进程锁（即使更换 LAB_CONTROL_HOME 仍互斥），并在 Windows AppData 视图重定向时解析到库存文件的最终物理运行根目录，阻止本软件 GUI/CLI/Python 实测进程同时占用仪器。锁被占用时启动边界报告持有进程的 PID 和启动时间；这些信息写入临时的 `hardware.lock.owner.json`，正常退出时清理，操作系统锁本身仍是唯一的占用依据。不关闭其他厂商软件，其他程序占用会产生可解释错误。模拟 CLI 和单元测试不获取硬件进程锁，存储隔离在 simulation。

## 数据流

    UI / CLI / Python
      -> ApplicationRuntime
         -> DiscoveryService -> provider 元数据 -> Inventory
         -> SessionManager -> 每台设备 worker -> InstrumentAdapter
                                                -> byte Transport 或 SDK
         -> Repository <- 身份、配置、状态、扫描、测量和审计
      <- Qt 信号 / Future / 事件快照

    控制页 / 监测页 / 卡片
      -> WorkspaceController -> MonitoringService（合并数据源、轮询、配置版本、记录）
                              -> SessionManager.submit -> 原设备 worker
      <- 每数据源最近批次 MonitorFrame -> LivePlot（显示抽稀）
      -> 显式记录 / 保存 -> 后台 writer -> Repository.export（原始批次）

扫描 worker 与设备 worker 独立。provider 并行枚举，身份探测使用有界线程池；原子租约按端点和别名隔离独立设备，并让安全探测共享兼容闸门。活动会话只提供缓存；未知别名延后探测，已有会话保持原句柄。扫描只关闭自己创建的临时对象。

## 线程和生命周期

每台仪器一个 ThreadPoolExecutor(max_workers=1)，完整操作串行。后台不操作 Qt 控件；ServiceBridge/WorkspaceController 传递 Future 结果。设备总览 300 ms 更新缓存，监测 UI 上限 10 Hz。MonitoringService 的调度线程只提交请求，不接触传输句柄；当前批读取完成后才执行排队参数，成功增加配置版本并继续。两个记录线程导出原始数据，每设备最多一项未完成记录写入，防止堆积。

退出先停止监测调度并等在途读取/记录完成，再关闭发现服务、等待设备 worker 完成并释放、关闭应用 VISA 管理器、SQLite 和进程锁。单台清理失败也继续其他清理。Pico 保留 buffer 到 SDK 停止并解除注册，Stop 失败仍尝试 CloseUnit。退出不发送输出关闭或恢复预设。

底层驱动/DLL 阻塞调用不能被 Python 线程强制中断；配置 I/O 超时并在实测中验证厂商驱动的最坏退出时间。当前不采用子进程强杀 SDK。

## 存储和依赖

根目录默认 %LOCALAPPDATA%/TF-LabControl，可用 LAB_CONTROL_HOME 指定；实验数据按 electronic/optical 分开。实际格式见[数据模型](../data/data-model.md)。本版数组导出为 NPZ/CSV；HDF5、完整实验索引及处理流水线尚未接入。

pyproject.toml/uv.lock 管理 PySide6、PyQtGraph、PyVISA、PySerial、NumPy；Ruff、mypy、pytest 为开发依赖。SDK/运行库独立安装、延迟加载，缺少某个 SDK 不影响其他设备或无驱动 UI 启动。

## 可测试边界

协议测试校验有来源的命令、单位、分帧和缩放；集成测试校验扫描/连接竞争、20 台模拟设备、存储、Qt 及 CLI；hardware 默认跳过。能力矩阵由实际注册表生成，软件和实机状态分离。构建检查需验证 wheel 中 QSS 与 py.typed，以及安装后 CLI/UI 入口。
