# 文档维护与本次迁移

## 唯一维护来源

使用指南写现行行为；需求写目标与实现边界；review/archive 保存特定时间的证据。型号协议、范围、固件问题、验证脚本及离线转换在型号包维护。派生卡片明确链接维护源，不手工改生成结果。

全部设备的现行说明采用固定章节和表格；新增测试更新对应的能力、接口、验证范围与已知问题，不在文末逐批追加段落。型号说明脱敏，资产现行页和原始批次分开维护，结构及生成入口见[记录规范](../testing/records.md)与[资产模板](../testing/templates/asset-status.md)。旧原始批次不可因整理当前结论而改写。

所有现行 Markdown 应能从根 README 经文档总入口到达。旧页带 `docs: redirect` 标记并链接新页；历史页带 `docs: historical`；模板带 `docs: template`。真实身份、地址、日志与测试数据禁止提交。

## 新增、修改和删除

新增设备按[接入流程](../testing/onboarding.md)，包内 README 使用[型号模板](../testing/templates/model-readme.md)。所有类别导航由注册元数据和现有待补卡片生成；型号不完整时保留待补卡片，不注册猜测适配器。

删除独立模块按[模块规范](../architecture/device-modules.md)撤销注册、删除包并重新生成。生成器只清理自己拥有且未被手工修改的卡片；历史实机记录不随源码删除。

## 检查入口

```powershell
.venv-dev\Scripts\python.exe scripts/generate_device_docs.py
.venv-dev\Scripts\python.exe scripts/generate_device_docs.py --check
.venv-dev\Scripts\python.exe scripts/check_docs.py
```

检查覆盖 UTF-8、本地文件与标题锚点、导航可达性、重复注册/卡片、型号维护源和模板结构。外部厂家链接不在离线检查中请求；有依据更新资料版本时另行核实。生成/检查不得创建 VISA 运行库或连接设备。

## 本次迁移清单

原文在新位置继续维护，旧位置仅保留跳转；相对链接已经换算。当前未提交修改随正文保留。设计基线和截图目录未移动。

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

历史软件验证数字移入 archive/software-validation-2026-09-23.md；原始需求快照移入 archive/requirements-2026-09-23.md，当前需求总览只维护现行边界。设备卡片和各类别导航由生成器刷新。

## 已知记录格式边界

已接入型号在各自模块提供离线转换器；未知格式、缺少身份/固件/范围/版本、空批次或损坏 JSON 保留索引并列为待核对。摘要只引用实际存在的阶段结果，不从目录名、输出关闭说明或上传目录推断控制、恢复或物理性能通过。
