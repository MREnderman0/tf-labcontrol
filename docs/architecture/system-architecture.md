# 系统架构与工程边界（0.3.0）

当前交付为 Python 模块化单体：真实通信、设备总览、分类控制和实时监测已接通；正式入口与内部 Mock 预览共用页面并注入隔离服务。自动实验、阈值告警和数据处理工作台仍在后续范围。

## 目录职责

| 路径（src/lab_control 下） | 职责 |
| --- | --- |
| domain/instruments.py | Qt 无关的身份、能力、参数、命令、结果和测量 |
| domain/connections.py、frames.py、recording.py | 连接值对象、不可变帧/容量校验及记录结果；不依赖应用/UI/存储 |
| domain/models.py | 原演示界面的展示模型 |
| transport/ | VISA/串口/TCP/VICP 分帧及 SDK 加载 |
| instruments/ | 基础适配器、独立型号 Profile、SDK 类和数据解析 |
| discovery/ | provider 枚举、有限探测、库存合并、取消与扫描记录 |
| discovery/types.py、providers.py、inventory.py | 发现类型、枚举与库存职责；service.py 保留扫描协调和旧导入兼容 |
| application/ | Runtime 生命周期、每设备 worker、租约、安全校验和事件 |
| storage/ | SQLite 库存/扫描、JSONL 日志、CSV/NPZ/Touchstone/报告 |
| cli/ | 环境诊断、扫描、只读测试、设置和采集 |
| ui/real_window.py、ui/controller.py | 共用应用窗口、总览、Qt 快照/Future 桥接 |
| ui/pages/device_control/ | 能力表单、单位、草稿、数组表格/预览、确认与结果 |
| ui/pages/monitoring/ | 类型页、PyQtGraph、响应式卡片总览 |
| application/monitoring.py、controls.py | Qt 无关的调度/记录、中文标签/页面分组/数据源元数据 |
| application/session_operations.py、recorded_acquisition.py | 原会话 worker 内的单次操作、连续采集循环与二进制收尾 |
| application/monitor_scheduler.py、frame_exports.py、monitoring_state.py | 原监测线程的调度、帧导出和状态类型；monitoring.py 保留公共入口 |
| storage/recording_workers.py、audit.py | 二进制消费与 CSV 子进程监督；有界审计失败证据 |
| ui/workspace/ | 导航、总览、动作、对话框、关闭协作模块；RealWindow 保留入口、控件与组装 |
| demo/workspace.py、demo/instrument.py | 24 台 A/B Mock 和参数相关模拟数据 |
| ui/main_window.py | 旧设计壳子，仅保留历史回归，不再作为启动入口 |

配置模板在 config/examples；用户使用示例在 examples；生成器和检查入口在 scripts；公共测试在 tests，型号测试及夹具在各型号包。根 conftest.py 覆盖两棵测试树，历史 tests/hardware 入口无条件跳过。厂商 DLL、临时下载和现场数据均在源码外。

## 运行入口

run.bat、Python 模块入口、lab-control 与安装版 EXE 统一调用 launcher.main；先分发 CSV worker、部署自检及 CLI，GUI 分支才进入 app.main 创建真实 ApplicationRuntime 和 RealWindow。内部测试和预览注入隔离 Mock ApplicationRuntime 并显示同一窗口；lab-control-cli 调用同一会话服务。配置文件只描述资源/适配器/驱动，不允许混入厂家命令。

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

每台仪器一个 ThreadPoolExecutor(max_workers=1)，完整操作串行。SessionOperations 和 RecordedAcquisitionRunner 在原 worker 内工作，不新建仪器线程。后台不操作 Qt 控件；ServiceBridge/WorkspaceController 传递 Future 结果。控制器每轮约 100 ms 统一读取快照，窗口 300 ms 更新只渲染缓存；显式刷新仍可请求新快照。MonitorScheduler 复用原监测线程和锁，只提交请求，不接触传输句柄；当前批读取完成后才执行排队参数，成功增加配置版本并继续。

连续记录使用公共有界缓冲和独立二进制消费线程，满时明确暂停并排空。CsvConversionSupervisor 管理独立 CSV 子进程，全局最多两个；CSV 转换不占仪器 worker。应用保留在途记录与最后一批结果，避免退出时重新遍历全部已完成历史；RecordingOutcome 区分已接收、已提交、已转换和错误，记录失败不能返回假成功。边界和格式见[监测规则](live-acquisition.md)。

退出先停止监测调度并等在途读取/记录完成，再关闭发现服务、等待设备 worker 完成并释放、关闭应用 VISA 管理器、SQLite 和进程锁。单台清理失败也继续其他清理。Pico 保留 buffer 到 SDK 停止并解除注册，Stop 失败仍尝试 CloseUnit。退出不发送输出关闭或恢复预设。

底层驱动/DLL 阻塞调用不能被 Python 线程强制中断；配置 I/O 超时并在实测中验证厂商驱动的最坏退出时间。当前不采用子进程强杀 SDK。

## 存储和依赖

根目录默认 %LOCALAPPDATA%/TF-LabControl，可用 LAB_CONTROL_HOME 指定。当前使用通用工作区；实验数据按设备、日期、采集开始时间分层，旧 electronic/optical 数据保持原样。单帧导出保留 NPZ/CSV，连续记录保存版本化 SQLite/BLOB 并异步转换 CSV；实际格式见[数据模型](../data/data-model.md)。HDF5、完整实验索引及处理工作台尚未接入。

Repository.audit 保留严格错误语义；恢复和清理路径使用 AuditSink.try_record 保存有界失败证据，避免审计磁盘错误遮盖原故障或阻断资源释放。不把审计失败解释为设备操作成功。

pyproject.toml/uv.lock 管理 PySide6、PyQtGraph、PyVISA、PySerial、NumPy；Ruff、mypy、pytest 为开发依赖。SDK/运行库独立安装、延迟加载，缺少某个 SDK 不影响其他设备或无驱动 UI 启动。

## 可测试边界

协议测试校验有来源的命令、单位、分帧和缩放；集成测试校验扫描/连接竞争、多台模拟设备、存储、Qt 及 CLI。统一[软件检查](../development/software-validation.md)不启用硬件；真实测试使用型号留档入口。能力矩阵由实际注册表生成，软件和实机状态分离。构建检查验证完整适用依赖闭包、wheel 中 QSS/py.typed、源码外入口及冻结 worker；正式发布仍须目标电脑和逐设备验收。
