# 统一采集节奏与显示帧保存验收

日期：2026-09-25。范围为公共监测控件、记录调度、显示帧导出及频谱视图；全部使用隔离 Mock，未连接或修改真实仪器。实机吞吐量不在本轮结论内。

## 当前行为

- 界面只有最快／自定义采集节奏及一个 0.5～60 秒间隔。记录从下一次记录读取开始，逐帧保存；旧显式数值保存间隔 API 保留兼容。
- CSV 转换固定 5 秒；缓冲满明确暂停记录、排空已接收帧，纯监测可继续。记录停止后参数锁不等待 CSV。
- 保存当前帧导出点击时实际显示的完整数组及其原时间、参数来源和校验级别。保存旧帧不访问仪器、不追加新采集、不锁定参数，不影响后台新帧。
- 频谱 Y 轴首次及参数变化后的首个有效帧适配一次，上下各留 10% 余量。手动适应同样只执行一次；总览频谱共用此规则，其他曲线保留原行为。
- 实际帧间隔取消悬浮说明，后台诊断仍保留。

## 自动化验证

| 项目 | 结果与证据 |
| --- | --- |
| 全项目测试 | 1464 passed、1 skipped，198.60 秒；包括原有硬件隔离与记录故障测试 |
| 修改相关回归 | 32 passed：显示帧导出、逐帧记录、频谱视图及真实 Qt 页面 |
| 记录完整性 | 最快和自定义模式均逐帧核对时间顺序及收到／提交／CSV 帧数；开始记录前已在途的监测帧不补存 |
| 保存语义 | 服务已有更新帧时仍保存显示帧；live/cached/full 原始标记保留，数据／坐标数组逐项相同；严格记录校验未放宽 |
| 并行与失败 | 慢导出时监测及参数应用继续；重复点击拒绝、磁盘错误不重试、退出排空。缓冲满后明确暂停，排空后释放参数锁 |
| 坐标轴 | 普通幅度变化不移动 Y 轴；参数版本、实际参数、单位、零跨度及重连触发适配；恒值有非零范围，无效帧不替换显示帧 |
| Ruff / Mypy | 全部通过；Mypy 检查 165 个源码文件 |
| 文档 | 派生生成一致性通过；链接、锚点、导航和来源检查通过 |
| 构建 | 离线 wheel、sdist 成功；wheel 独立导入及七个型号逐一移除检查通过 |

首轮全量运行出现一次 Windows Qt 原生访问异常（pytest-qt 处理事件阶段），该轮未计为通过；保留原日志后重新完整运行，得到上述通过结果。相关回归及三档 DPI 渲染均独立完成，不能据此宣称已定位或修复该原生异常。

本轮检查摘要见 [validation.json](../../design/reviews/unified-monitoring/validation.json)。历史采集方案和性能报告保持原结论，当前交互以[使用指南](../guides/control-and-monitoring.md)和[公共规则](../architecture/live-acquisition.md)为准。

## 视觉验证

用正式 `RealWindow`、唯一主题及 `hardware=False` 的临时运行目录渲染。共 12 张：1280×760／1440×900 逻辑像素，100%／125%／150% DPI，最快／自定义两种模式。逐张检查单行排列、输入框对齐、文字完整、控件边界和曲线范围，无新增裁切或换行；原参数区按需纵向滚动。自动化同时验证草稿与输入焦点保留、当前帧按钮状态和无帧间隔浮窗。

| DPI | 最小窗口示例 | 默认窗口示例 | 几何及行为记录 |
| --- | --- | --- | --- |
| 100% | [最快](../../design/reviews/unified-monitoring/spectrum-fastest-1280x760-1.png) | [自定义](../../design/reviews/unified-monitoring/spectrum-interval-1440x900-1.png) | [JSON](../../design/reviews/unified-monitoring/render-1.json) |
| 125% | [最快](../../design/reviews/unified-monitoring/spectrum-fastest-1280x760-1.25.png) | [自定义](../../design/reviews/unified-monitoring/spectrum-interval-1440x900-1.25.png) | [JSON](../../design/reviews/unified-monitoring/render-1.25.json) |
| 150% | [最快](../../design/reviews/unified-monitoring/spectrum-fastest-1280x760-1.5.png) | [自定义](../../design/reviews/unified-monitoring/spectrum-interval-1440x900-1.5.png) | [JSON](../../design/reviews/unified-monitoring/render-1.5.json) |

可用 [渲染脚本](../../scripts/review_monitoring_cadence.py) 分别传入 `--scale 1`、`--scale 1.25`、`--scale 1.5` 复现。脚本不开始记录、不导出数据、不扫描或连接真实设备。
