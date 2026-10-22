<!-- docs: historical -->
# 菜单、长单位与已连接设备筛选验收

日期：2026-09-24。沿用已确认的深色琥珀分类卡片和总览内居中设备池。

## 实现

- 设备池底部按连接／断开、功能槽位、设备控制、实时监测、更多排列。连接为琥珀实心，其他为一致的深色描边；操作区可换行，采集停止操作独立保留。功能槽位复用原绑定与解除绑定接口、忙碌限制。
- `WorkspaceComboBox` 和 `WorkspaceMenu` 共用深色浮层绘制，8 逻辑像素圆角、细描边、柔和阴影、6 像素内边距、34 像素选项行高、13 像素字体。选择项显示淡琥珀底及勾选；悬停浅灰、禁用项减弱、分隔线与滚动条沿用统一主题。
- 单位控件取消重复的箭头预留：保留独立箭头区，以最长候选文字、实际字体和 Qt 样式返回的文本矩形计算最小宽度。字体、样式和设备像素比变化会重新计算。未改单位换算、范围校验和草稿规则。
- 紧凑面板不再覆盖单位宽度。提示文字变化后的行高同时遵守子控件实际最小高度，避免 Qt 高度缓存尚未更新时限幅提示挤压下一行。
- 界面控制器统一使用缓存会话筛选：`connected`、`busy`、`degraded` 可选择，异常会话标注状态；未连接、连接中、失败及纯库存记录不列出。没有额外硬件查询，也没有设备服务接口或存储格式变化。
- 当前设备失效即关闭展开的设备菜单、清空选择、通道和参数并隐藏输出面板，停止该页面的自动读回关注；不自动跳到另一台。重连后需手动选择；失效程序跳转也保持空态。总览采样选择、槽位和历史数据保留。

## 验证

最终完整软件回归 **669 项通过，1 项实机测试跳过**（103.76 秒）。Ruff、修改文件格式检查、mypy（107 个源文件）及 `git diff --check` 通过。DPR 1.25 和 2 下额外执行菜单与单位专项测试，各 30 项通过；四档 DPR 的实际截图及尺寸均已核对。

本验收页及截图链接已核对。末次全仓 Markdown 检查时，另有文档目录迁移中的 4 处链接错误，位于 `archive/requirements-2026-09-23.md` 和 `testing/hardware-testing.md`；保留该并行修改，不计为本次 UI 软件回归失败。

使用临时库存、Mock 通信及缓存状态注入。覆盖有效／忙碌／异常会话、连接中／失败排除、弹层展开时断开、失效跳转、重新连接、空态返回总览，以及监测总览设备卡片的连接状态筛选和槽位选择保留。

菜单交互检查覆盖点击、键盘、滚轮和滚动条、外部取消、Esc 分层关闭、设备池子对话框、当前选择标记、禁用项、稳定选择与刷新。单位测试覆盖全部公共单位、`mVpp`、`mV/div`、`V/div`、`MHz`、长型号单位和字体／样式变化；500 mVpp 仍为 0.5 Vpp，草稿正确且模拟参数写入计数为零。

截图使用实际 Qt 控件和浮层渲染，offscreen 平台下分别设置并核对 **实际 DPR 1 / 1.25 / 1.5 / 2**。窗口包括 1440×900、1100×760、1024×600、800×560；设备池额外检查常规与小窗口、详情滚动和按钮边界。截图由代理目视检查，元数据记录实际 DPR、文本区域、文字宽度、菜单逐行高度及屏幕位置。

| 实际 DPR | 长单位与菜单 | 小窗口设备池 |
| --- | --- | --- |
| 1 | [幅度单位](../../design/reviews/ui-polish/dpr-1/amplitude-800.png)、[全部长单位](../../design/reviews/ui-polish/dpr-1/units-300.png) | [设备池](../../design/reviews/ui-polish/pool-1/pool-1000-current.png) |
| 1.25 | [幅度单位](../../design/reviews/ui-polish/dpr-1.25/amplitude-800.png)、[动作菜单](../../design/reviews/ui-polish/pool-1.25/pool-1440-menu.png) | [设备池](../../design/reviews/ui-polish/pool-1.25/pool-1000-current.png) |
| 1.5 | [波形](../../design/reviews/ui-polish/dpr-1.5/waveform-1440.png)、[长单位](../../design/reviews/ui-polish/dpr-1.5/units-300.png) | [设备池](../../design/reviews/ui-polish/pool-1.5/pool-1000-current.png) |
| 2 | [幅度单位](../../design/reviews/ui-polish/dpr-2/amplitude-800.png)、[长单位](../../design/reviews/ui-polish/dpr-2/units-300.png) | [设备池](../../design/reviews/ui-polish/pool-2/pool-1000-current.png) |

[断开空态](../../design/reviews/ui-polish/dpr-1/disconnected-empty.png)。每个截图目录内的 `metrics.json` 保留实测尺寸；单位控件原文字区 33 像素现为至少 57 像素，长候选自动扩宽。控制页的两列结构保持不变。

复现示例：

```powershell
.venv-dev/Scripts/python.exe scripts/review_parameter_popups.py .tools/ui-review --dpr 1.25 --kinds amplitude waveform
.venv-dev/Scripts/python.exe scripts/review_device_pool.py .tools/pool-review --dpr 1.25
```

本轮没有访问真实设备，没有扫描或改动现场库存库、实机历史。SMA100B 历史／当前发现判定和活动会话保护继续由隔离扫描回归验证；离线结果不代替现场库存核对。Qt 自动化及截图不代表真实设备验收，也未验证多显示器热插拔。
