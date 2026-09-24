# 项目文档

当前 0.3.0 已接通真实兼容层、总览、分类控制、实时监测与 CLI/Python 实测入口；Mock 仅供内部测试与预览，使用相同页面和隔离服务。安装启动见[根 README](../README.md)。

| 文档 | 用途 |
| --- | --- |
| [设备索引](device-research-index.md) | 附件每个条目的唯一归档入口 |
| [实际能力矩阵](generated/capability-matrix.md) / [JSON](generated/capability-matrix.json) | 从真实注册表生成，区分实现、协议夹具、选件与实机状态 |
| [实机测试指南](hardware-testing.md) | 驱动、安装、配置、CLI、采集、故障及验收记录 |
| [实机测试记录规范](hardware-test-records.md) | 每台、每次必须留档；逐项结果、恢复、输出状态与资产台账 |
| [开发环境与下载策略](development-environment.md) | 国内镜像、禁用代理、项目 uv 与 PyVISA 优先接入约定 |
| [输入范围规则](input-ranges.md) | 实机会话快照、范围来源、前端限幅和后端严格校验 |
| [设备发现与总览](requirements/device-discovery-and-overview.md) | 扫描与总览的唯一详细设计来源 |
| [设备管理](requirements/device-management.md) | 总览行为与操作边界 |
| [控制与监测指南](control-and-monitoring.md) | 设备选择、分类调参、监测卡片、记录与导出 |
| [信号源界面验收](ui-signal-source-review.md) | 紧凑面板、低分辨率适配、功能验证与独立视觉审查 |
| [下拉菜单与启动入口验收](ui-popup-launch-review.md) | 弹出高度、首次显示事件、单一入口与菜单截图 |
| [实时监测设计](requirements/realtime-monitoring.md) | 布局、调度、配置版本、共享采集与记录边界的当前依据 |
| [兼容层](protocols/device-compatibility.md) | 领域接口、型号注册、校验、采集和扩展规范 |
| [设备模块维护规范](architecture/device-modules.md) | 每型号独立包、唯一注册、测试与说明、新增和删除流程 |
| [传输协议](protocols/visa-scpi.md) | VISA 生命周期、串口/TCP/VICP/SDK 和分帧 |
| [系统架构](architecture/system-architecture.md) | 路径、服务、worker、应用退出和依赖 |
| [数据模型](data/data-model.md) | 实际 SQLite/JSONL/CSV/NPZ/Touchstone/报告格式 |
| [设备分类](devices/README.md) | 各类别型号卡片 |
| [软件验证](protocols/mock-visa-validation.md) | 测试层级、夹具来源与检查命令 |
| [原始需求拆分](requirements.md) | 采集、监测、处理等后续工作范围 |

型号卡片和实际矩阵由 scripts/generate_device_docs.py 生成，现包含控制页面、中文标签与分组位置。独立型号以包内 README 和注册元数据为维护源；更新后重新生成，不手工更改派生卡片或操作清单。历史产品需求不代表全部处理/自动实验功能已实现；0.3.0 的实际边界以上表为准。

附件只作为设备清单和功能需求输入。空白载荷协议表、历史日期和疑似资产编号不作为已确认接口。具体资产的实机状态以运行目录留档为准；软件兼容或只读连接通过不代表整机验收通过。

- [电源四宫格操作与验收](ui-power-supply-review.md)

- [频谱仪紧凑监测与标记](ui-spectrum-review.md)
