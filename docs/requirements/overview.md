# 需求与实现边界

当前支持真实兼容层、设备发现、分类控制、实时监测及 CLI/Python 入口。型号实现见[能力矩阵](../generated/capability-matrix.md)，具体资产的测试结论见运行目录台账。连接或控制通过不代表物理性能验收。

## 当前需求来源

- [设备发现与总览](device-discovery-and-overview.md)
- [设备管理](device-management.md)
- [实时监测](realtime-monitoring.md)
- [日志和设置](logging-and-settings.md)
- [数据处理](data-processing.md)

## 后续能力

自动实验、阈值告警、完整数据处理及型号未注册功能仍需单独规划；需求文字不代表已交付。当前持久化采用 SQLite、JSONL、CSV、NPZ 等，实际格式以[数据模型](../data/data-model.md)为准。

[原始需求快照](../archive/requirements-2026-09-23.md)保留设备需求来源与历史设想。当前交互以专题需求和[确认的设计基线](../../direction-approved.md)为准。
