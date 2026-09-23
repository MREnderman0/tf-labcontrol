# 项目文档

当前 0.3.0 已接通真实兼容层、总览、分类控制、实时监测与 CLI/Python 实测入口；演示入口用相同页面和隔离 Mock 服务。安装启动见[根 README](../README.md)。

| 文档 | 用途 |
| --- | --- |
| [设备索引](device-research-index.md) | 附件每个条目的唯一归档入口 |
| [实际能力矩阵](generated/capability-matrix.md) / [JSON](generated/capability-matrix.json) | 从真实注册表生成，区分实现、协议夹具、选件与实机状态 |
| [实机测试指南](hardware-testing.md) | 驱动、安装、配置、CLI、采集、故障及验收记录 |
| [设备发现与总览](requirements/device-discovery-and-overview.md) | 扫描与总览的唯一详细设计来源 |
| [设备管理](requirements/device-management.md) | 总览行为与操作边界 |
| [控制与监测指南](control-and-monitoring.md) | A/B 演示、分类调参、监测卡片、记录与导出 |
| [实时监测设计](requirements/realtime-monitoring.md) | 布局、调度、配置版本、共享采集与记录边界的当前依据 |
| [兼容层](protocols/device-compatibility.md) | 领域接口、型号注册、校验、采集和扩展规范 |
| [传输协议](protocols/visa-scpi.md) | VISA 生命周期、串口/TCP/VICP/SDK 和分帧 |
| [系统架构](architecture/system-architecture.md) | 路径、服务、worker、应用退出和依赖 |
| [数据模型](data/data-model.md) | 实际 SQLite/JSONL/CSV/NPZ/Touchstone/报告格式 |
| [设备分类](devices/README.md) | 各类别型号卡片 |
| [软件验证](protocols/mock-visa-validation.md) | 测试层级、夹具来源与检查命令 |
| [原始需求拆分](requirements.md) | 采集、监测、处理等后续工作范围 |

型号卡片和实际矩阵由 scripts/generate_device_docs.py 生成，现包含控制页面、中文标签与分组位置；编辑生成器、展示元数据与注册表后重新生成，不手工更改操作清单。历史产品需求不代表全部处理/自动实验功能已实现；0.3.0 的实际边界以上表为准。

附件只作为设备清单和功能需求输入。空白载荷协议表、历史日期和疑似资产编号不作为已确认接口。所有设备实机状态均待现场验收；没有宣称已接线验证。
