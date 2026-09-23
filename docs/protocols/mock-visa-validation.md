# 软件验证与夹具

测试目录按 unit、integration、fixtures/protocols/<adapter>/control.json、hardware 分层。命令夹具由厂家资料编写，不是实机录制；原始来源见对应型号卡片和生成的 JSON 能力矩阵。厂商 DLL 使用类型化函数替身，不需真实硬件。

## 覆盖内容

- 领域：参数类型、单位/范围、确认、未知操作、精确型号/后缀和选件。
- 传输：TCP 分包/粘包、IEEE 块截断/长度、字节序、VICP 头、多包、共享 VISA 生命周期。
- 型号：各注册 SCPI 型号至少一条独立命令夹具；AFG/SDG 任意波形编码、SMA 列表、GPD 状态、RIGOL 缩放、复数轨迹、TSERVE MHz/ADEV。
- SDK：DLL 缺失、错误码、PM100D/TLPMX 通道 ABI 与 reset=0、Pico block/streaming、缓冲区释放及 Stop 失败。
- 应用：20 台模拟设备全部开始持续采集后连续扫描 3 次，保持原会话并持续产出批次；原子别名租约、连接竞争、取消、provider 失败、增量合并、SQLite、中文配置/数据和进程互斥。
- UI/CLI：独立演示、固定类别、模式不隐藏设备、行/选择保持、显式资源、只读报告和采集导出。

## 执行

    uv run ruff check src tests scripts examples
    uv run ruff format --check src tests scripts examples
    uv run mypy
    uv run pytest -q
    uv run python scripts/generate_device_docs.py --check
    uv run python scripts/check_docs.py
    uv build

pytest 使用临时目录，Qt 为 offscreen。hardware 默认跳过；启用条件见[实机指南](../hardware-testing.md)。软件测试不能证明仪器固件、接线、驱动选件或所有高级命令组合正确。矩阵中的 protocol_fixture_tested 只标记实际独立命令夹具覆盖的操作，不能把通用参数测试写成所有命令已通过实测。

## 0.2.0 历史验证记录（2026-09-23）

验证环境为 Windows x64 / Python 3.12.11，测试与实测工具使用临时数据目录。没有连接或验收真实仪器。

| 检查 | 结果与边界 |
| --- | --- |
| pytest | 122 项通过，1 项硬件测试默认跳过；包含 20 台并行采集时的会话保护 |
| Ruff / mypy | 格式与规则检查通过；50 个源文件类型检查通过 |
| 文档 | 注册表生成结果一致；本地 Markdown 链接和 UTF-8 检查通过 |
| 实测入口 | `scan-stress` 在 6 台模拟设备上保存扫描前后会话及采集路径；不把空采集或取消扫描记为通过 |
| Python 示例 | 信号源读写与波形上传、电源、示波器/频谱采集、Pico streaming、光功率、ZNB Touchstone 均在模拟模式运行通过 |
| 分发包 | wheel 与 sdist 构建通过；wheel 包含两套 QSS 与 py.typed，不含 DLL/实测数据；sdist 包含文档、配置、示例及嵌套测试夹具 |
| 独立安装 | 将 wheel 安装到新虚拟环境，在源码目录之外运行 CLI 帮助/型号/模拟报告和真实总览启动退出，确认从 site-packages 加载 |
| 视觉检查 | 离屏检查真实总览的深色控件、类别卡片、设备行和详情抽屉，原演示界面保留 |

SDK 错误和数据用替身验证，型号命令用厂家资料编写的夹具验证。只有现场核对身份、参数读回、波形/数值、断连恢复及物理输出后，才能提升对应能力的实机状态；部分高级功能的未实现项继续记录在[能力矩阵](../generated/capability-matrix.md)。

## 0.3.0 分类控制与监测验证

Windows x64 / Python 3.12.11 / PySide6 6.11.2 / PyQtGraph 0.14.0。未连接真实设备。

| 检查 | 结果与边界 |
| --- | --- |
| pytest | 136 项通过，1 项硬件测试跳过；保留既有协议与 CLI 测试 |
| 新增调度验证 | 20 台并行监测/扫描/调参、原会话保持、整批读取后才应用、同源通道合并、失败隔离与版本过滤 |
| 数据与记录 | 纯监测无实验文件；显式记录保留原始批次；停止记录完成在途批次，模式锁等待结束 |
| 参数界面 | 全部注册能力有中文标签/页面位置；单位与 OFF 布尔读回、输入中草稿保护、确认取消、部分执行保留、中文 CSV 预览 |
| 页面与布局 | 12 类共用真实/Mock 页面、24 台 A/B 演示；模式配置独立、标题定位通道、1 列至 4 列以上、缩放复用卡片与任务 |
| 工程检查 | Ruff、mypy（64 个源文件）、UTF-8 Markdown 链接与生成文档一致性检查通过 |
| 安装检查 | 独立虚拟环境、源码外从 site-packages 加载 0.3.0；真实空库存启动、Mock 类型页与监测、CLI 报告、QSS/py.typed 资源检查 |
| 视觉检查 | 信号源/电源表单、示波器/频谱类型页、监测总览在 760/1450/1900 逻辑像素窗口离屏检查 |

Mock 随部分已声明参数改变波形、时间轴、单位、输出测量或趋势，模拟质量标签保留。测试不证明厂家固件/SDK 在现场的所有参数组合；全部真实型号仍待实机验收。
