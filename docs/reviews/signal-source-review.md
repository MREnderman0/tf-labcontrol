# 信号源紧凑面板验收记录

日期：2026-09-23。深色琥珀分类卡片基线提交为 `bba21b7`；本轮只改信号源控制页、必要的通用输入行为和窗口尺寸约束。首页卡片、导航和配色继续沿用已确认基线。

## 本轮最终范围

- 单通道射频源集中显示频率、功率、参考源和独立 RF 输出；功率偏移、RF 限幅只读展示。
- 双通道并排、四通道两行；窗口变窄仍保持双列。侧栏不自动收起。
- 应用界面固定 100%，未加入缩放设置或偏好服务；暂不提供步长和加减按钮。Qt 继续处理系统 DPI。
- 常见工作区最小窗口为 960×600 逻辑像素；工作区不足时按实际可用区域调整，包括任务栏和窗口边框。启动、换屏或屏幕工作区变化时重新约束窗口位置与尺寸。较低高度通过纵向滚动访问参数和按钮。
- 读取和应用按钮统一为 110×38 逻辑像素。没有修改时应用按钮灰色禁用，修改后亮起，改回原值后再次变灰；单位等价转换不算参数修改。
- 模拟模式隐藏重复回读行。Mock 应用成功后的设置与 Mock 存储一致；真实设备仍保留独立实际读回，不截断超出当前输入范围的真实值。
- NaN、无穷大、空值、非法文本或非整数的整数输入不保存；失焦或回车时静默恢复上一次有效值，没有历史有效值则留空。有限但超界的数字仍限幅并显示修正提示。后端和 CSV/TXT 导入继续严格校验。

## 功能验证

使用隔离 Mock 服务、协议夹具及 Qt 自动化测试，不打开物理传输。新增信号源测试覆盖以下行为：

| 检查项 | 结果 |
| --- | --- |
| 通道草稿隔离、读取不覆盖未提交草稿、仅提交当前通道已修改项 | 通过 |
| 分组逐项等待、部分失败停止、已完成/失败/未执行分别提示 | 通过 |
| 关联参数更新范围，修正剩余草稿后暂停等待再次应用 | 通过 |
| RF 输出单独确认，参数应用不包含输出命令 | 通过 |
| 无修改/改回原值/成功应用后的按钮状态、模拟存储与输入一致 | 通过 |
| 非法输入静默恢复，未持久化非法文本，编辑和恢复不写设备 | 通过 |
| 范围修正提示展开后不覆盖下一参数行 | 通过 |
| 小工作区内窗口边界、侧栏保持、双列保持、按钮可滚动访问 | 通过 |

原有范围测试继续覆盖负 dBm、科学计数法、最终单位、动态范围、通道边界和后端严格拒绝越界；原有监测测试验证页面/窗口变化不重建会话、不停止监测。

最终回归：包含其他任务未提交型号适配的工作区为 **187 passed, 1 skipped**；将本次暂存内容导出到独立目录、排除其他任务修改后为 **166 passed, 1 skipped**。暂存版本的 Ruff、Mypy（67 个源码文件）、生成文档检查（0 变化）及本地文档检查（61 份、0 错误）通过。跳过项为默认不执行的实机测试；未执行实机参数写入、RF 开启或物理输出测量。

## 独立视觉审查

在功能测试之外，单独运行截图脚本并逐张检查实际渲染结果。截图不是历史批准基线替代件，保存在 `design/reviews/signal-source` 供用户审阅。

所有截图使用隔离 Mock。SMA100B 预览注入 102 MHz、+21 dBm、EXT、RF OFF，频率 8 kHz～12.75 GHz、功率 −145～+40 dBm、输出限幅 +20 dBm；“实机范围”字样是用于检查此显示状态的模拟能力快照，不代表此次重新查询仪器。

| 场景 | 预览 | 审查结果 |
| --- | --- | --- |
| SMA100B，1440×900 | [射频源](../../design/reviews/signal-source/rf-1440-100.png) | 主区、范围、只读限幅、参考源和操作按钮首屏可达 |
| SMA100B，1100×760 | [射频源小窗口](../../design/reviews/signal-source/rf-1100-100.png) | 字段可读，主区完整，高级区可滚动 |
| AFG31252，1440×900 | [双通道](../../design/reviews/signal-source/awg-1440-100.png) | 两列对齐，无重复模拟回读行 |
| AFG31252，1100×760 | [双通道小窗口](../../design/reviews/signal-source/awg-1100-100.png) | 两通道常用参数与等大按钮首屏完整 |
| 1366×728 可用工作区 | [低分辨率](../../design/reviews/signal-source/awg-workarea-1366x728.png) | 窗口边框计入可用空间，侧栏保持展开 |
| 1024×600 可用工作区 | [短屏](../../design/reviews/signal-source/awg-workarea-1024x600.png) | 双列保持，较低内容区通过纵向滚动访问 |
| 800×560 可用工作区 | [更小工作区](../../design/reviews/signal-source/awg-workarea-800x560.png) | 侧栏和双列保持，按钮区内部换行，不产生横向裁切 |
| 修改但未应用 | [待应用状态](../../design/reviews/signal-source/pending-draft.png) | 当前通道按钮亮起，另一通道保持灰色 |
| 非法输入离开输入框 | [静默恢复](../../design/reviews/signal-source/invalid-restored.png) | 恢复有效值，无错误行和新增设备写入 |
| 缺少调制选件 | [不可用功能](../../design/reviews/signal-source/modulation-unavailable.png) | 不可用参数禁用并保留原因，RF 状态仍可见 |
| 四通道 | [两行双列](../../design/reviews/signal-source/four-channel.png) / [小窗口](../../design/reviews/signal-source/four-channel-narrow.png) | 保持两列，输出状态常驻，后两通道通过滚动访问 |

本次视觉修正包括：清理空状态占位导致的留白、任务页只按当前标签计算高度、单位框避免文字裁切、动态提示重新计算网格行高、通道标题顶部对齐、统一按钮尺寸和灰色禁用样式。较短屏幕允许滚动，不承诺所有高级参数同时位于首屏。

工作区尺寸按 Qt 逻辑像素验证；Windows 系统缩放仍由 Qt 处理，没有重复乘算比例。不同显示器组合的原生窗口装饰与跨屏拖动仍建议现场复核；本次没有声称完成所有实体显示器验收。

## 复现

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
.venv-dev\Scripts\python.exe -m pytest -q
.venv-dev\Scripts\python.exe -m ruff check src tests scripts
.venv-dev\Scripts\python.exe -m mypy
.venv-dev\Scripts\python.exe scripts/generate_device_docs.py --check
.venv-dev\Scripts\python.exe scripts/check_docs.py
.venv-dev\Scripts\python.exe scripts/review_signal_ui.py .tools/signal-ui-review/current
```

本次未下载新依赖，复用 `.venv-dev`。下载仍遵循[国内镜像与禁用代理约定](../development/environment.md)。其他任务的型号适配修改独立保留，不混入本轮信号源 UI 提交。
