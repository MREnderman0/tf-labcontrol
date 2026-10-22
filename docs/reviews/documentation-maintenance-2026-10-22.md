<!-- docs: historical -->
# 文档入口整理记录（2026-10-22）

本页记录本轮文档结构整理，不代表软件全量回归或真实设备验收。现行维护约定见[文档维护](../development/documentation.md)，操作说明见[使用指南](../guides/control-and-monitoring.md)。

## 整理范围

根 README 收敛为定位、源码/目录式测试包启动、快速开始和任务导航；默认配置与资源选择说明补入使用指南，其他重复说明改为链接。旧 README 章节锚点继续指向对应导航项。

电源四宫格与频谱界面原批次评审迁入 reviews，保留完整证据正文和截图原位置，旧文件仅保留跳转及原标题锚点。其他历史评审补齐 historical 标记，原批次结果和数字不改写。

## 兼容入口快照

下表从文档治理页迁出，记录整理时可用的兼容映射。前十四项为已有迁移结果，最后两项为本轮评审归位；此表不表示本轮重新执行旧验收或改写实机历史。

| 旧入口 | 当前维护位置 |
| --- | --- |
| `docs/control-and-monitoring.md` | [当前文档](../guides/control-and-monitoring.md) |
| `docs/input-ranges.md` | [当前文档](../guides/input-ranges.md) |
| `docs/development-environment.md` | [当前文档](../development/environment.md) |
| `docs/open-source-references.md` | [当前文档](../development/references.md) |
| `docs/protocols/mock-visa-validation.md` | [当前文档](../development/software-validation.md) |
| `docs/hardware-testing.md` | [当前文档](../testing/hardware-testing.md) |
| `docs/hardware-test-records.md` | [当前文档](../testing/records.md) |
| `docs/requirements.md` | [当前文档](../requirements/overview.md) |
| `docs/ui-connected-selection-review.md` | [当前文档](../reviews/connected-selection-review.md) |
| `docs/ui-device-pool-review.md` | [当前文档](../reviews/device-pool-review.md) |
| `docs/ui-popup-launch-review.md` | [当前文档](../reviews/popup-launch-review.md) |
| `docs/ui-signal-source-review.md` | [当前文档](../reviews/signal-source-review.md) |
| `docs/ui-signal-sync-review.md` | [当前文档](../reviews/signal-sync-review.md) |
| `docs/ui-workspace-combos-review.md` | [当前文档](../reviews/workspace-combos-review.md) |
| `docs/ui-power-supply-review.md` | [原批次评审](../reviews/power-supply-review.md) |
| `docs/ui-spectrum-review.md` | [原批次评审](../reviews/spectrum-review.md) |

历史软件验证数字移入 archive/software-validation-2026-09-23.md；原始需求快照移入 archive/requirements-2026-09-23.md，当前需求总览只维护现行边界。设备卡片和各类别导航由生成器刷新。

## 验证边界

本轮结构检查覆盖 Git 可见 Markdown 的本地链接、标题锚点、导航、历史/模板标记、跳转目标和现行命令规范；型号/下载资源派生页另做生成一致性检查。外部链接、物理设备与目标电脑交付不由这些检查证明。完整工程验证结果以对应检查 JSON 报告为准，不将持续变化的回归数量写成本页现行结论。
