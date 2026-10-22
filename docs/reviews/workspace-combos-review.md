<!-- docs: historical -->
# 统一下拉菜单验收与维护

日期：2026-09-24。保留 `0ae6721` 确认的深色琥珀分类卡片及紧凑信号源页面。本次仅验证界面，使用临时目录中的隔离 Mock；未扫描、连接或写入真实设备。此前 SMA100B 扫描修复及其余未提交工作保留。

本文保留此前菜单修复和参数字号的历史验收记录。最新菜单外观（8 像素圆角、34 像素行高）、长单位宽度和已连接筛选，以[后续验收记录](connected-selection-review.md)为准。

## 统一组件与行为

- 应用自定义下拉框统一使用 `ui/widgets/workspace_combo.py` 中的 `WorkspaceComboBox`。`ParameterComboBox` 保留原导入入口，继续用于参数及单位；资源、槽位选择对话框使用 `choose_item`，返回 `(文本, 是否确认)`。
- 覆盖设备、通道、高级通道、单位、波形、运行模式、类别与状态筛选、手动添加设备、监测项目及对话框。保留 Qt 的项目数据和选择信号，不改设备协议、范围或会话接口。
- 箭头由 Qt 矢量绘制；边框圆角 4 个逻辑像素，选项行高 32，最多显示 8 行。悬停/键盘定位使用浅灰底，当前已选项以琥珀文字及侧标记区分。短列表全部显示，长文本超过屏幕宽度时省略。
- 弹层显示前确定尺寸及位置，优先向下，空间不足时向上，屏幕边缘按完整行收缩。关闭 Qt 原生菜单边缘自动滚动；普通滚轮和触摸板像素滚动只作用于列表，到边界后不传给页面。
- 悬停、滚动和上下键浏览不提交；点击或 Enter 提交，Esc、Tab 或外部点击取消。取消点击不重放给下面的按钮。未展开时滚轮转给所在页面，不切换选项。
- 窗口移动或缩放时关闭菜单；内部提示文字的布局刷新不打断选择，不把数值草稿按旧单位提前限幅。
- 异步列表更新必须使用 `replace_items`，以项目数据中的稳定标识匹配。改名、增加、排序在关闭后应用；移除条目时立即关闭，当前设备被移除则显示“设备不可用 / 未选择”，不会自动切到另一设备。直接修改模型会取消菜单及待应用的旧刷新，避免模型修改重入。

新增选择控件沿用上述组件和唯一主题 `card_workspace.qss`，不要重新引入裸 `QComboBox()` 或 `QInputDialog.getItem()`；测试会检查这两类遗漏。型号专属逻辑仍只放在各设备模块。

## 自动化验收

`tests/integration/test_workspace_combos.py` 与 `test_parameter_popups.py` 共 32 项，覆盖：

- 短/长/空列表、禁用项、超长文本、首次展开与重复展开、当前项位于末尾、屏幕上下及右边缘。
- 滚轮及像素滚动、上下边界、鼠标边缘停留、滚动条拖动、键盘确认/取消和外部点击；滚动不改变已选项或背后页面位置。
- 展开期间模拟库存轮询、设备改名、排序、增加、移除和直接模型清空，核验位置、滚动位置、设备身份及信号结果。
- 正式 `RealWindow` 中所有应用下拉框使用统一组件；冷启动建页不产生额外顶层窗口。
- 先输入 500 再选择 mVpp，最终仍为 0.5 Vpp，草稿正确且 Mock 参数写入计数为零。选择无需等待旧原生菜单的 600 ms 防重击时间。

完整软件回归 **498 项通过，1 项实机测试跳过**。Ruff 静态检查、mypy、本次文件格式检查、生成文档一致性及 Markdown 检查通过。

全仓格式检查另报 4 个本次未修改的既有文件：`docs/protocols/device-compatibility.md`、`scripts/check_device_wheel.py`、`tests/unit/test_adapters.py`、`tests/unit/test_extended_protocols.py`。本次未扩大修改这些文件。

## 原生 Windows 与截图

使用 `QT_QPA_PLATFORM=windows`，通过 QtTest 自动发送事件，按最终设备像素比 **1.0 / 1.25 / 1.5** 分别运行上述 32 项检查。缩放仅设置于验证子进程，不修改系统显示设置或正式应用配置。

截图由 `scripts/review_parameter_popups.py` 抓取正式页面与实际弹层，再按控件位置合成。每组 `metrics.json` 保存平台、实际像素比、窗口/菜单坐标、视口及逐行高度；截图中的设备及能力来自 Mock，不是实机状态。

| 最终缩放 | 波形菜单 | 其他场景 |
| --- | --- | --- |
| 100% | [波形](../../design/reviews/workspace-combos/windows-100/waveform-1100.png) | [设备](../../design/reviews/workspace-combos/windows-100/device-1100.png)、[幅度单位](../../design/reviews/workspace-combos/windows-100/amplitude-1100.png)、[时间单位](../../design/reviews/workspace-combos/windows-100/time-1100.png) |
| 125% | [波形](../../design/reviews/workspace-combos/windows-125/waveform-1100.png) | [幅度单位](../../design/reviews/workspace-combos/windows-125/amplitude-1100.png) |
| 150% | [波形](../../design/reviews/workspace-combos/windows-150/waveform-1100.png) | [幅度单位](../../design/reviews/workspace-combos/windows-150/amplitude-1100.png) |

本机 Windows 原生像素比为 1.5，`QT_SCALE_FACTOR` 会与其相乘，因此验证分别使用 `0.6666666666666666`、`0.8333333333333334`、`1`，并检查截图元数据中的最终比值，不以环境变量名称代替实测值。

原生测试的 faulthandler 日志曾打印 `0x8001010d` Windows 异常；不导入项目代码的空白 Qt 窗口也出现相同日志并返回 0。各验证进程均按实际退出码及测试汇总判定，保留该环境现象，不将其解释成已修复。原始执行日志保存在本地 `.tools/ui-combos/`，可交付汇总见 [verification.json](../../design/reviews/workspace-combos/verification.json)。

以上交互验证为自动化 Qt 事件，截图由代理目视检查；**没有声称用户已用实体鼠标/触摸板人工验收，也未验证多显示器热插拔**。

复现原生测试（先按当前机器原生像素比换算 `QT_SCALE_FACTOR`）：

```powershell
$env:QT_QPA_PLATFORM='windows'
$env:QT_SCALE_FACTOR='0.6666666666666666'
.venv-dev/Scripts/python.exe -m pytest tests/integration/test_workspace_combos.py tests/integration/test_parameter_popups.py -q
.venv-dev/Scripts/python.exe scripts/review_parameter_popups.py .tools/ui-combos-review --window 1100 760 --kinds device amplitude waveform time
```

## 后续更新：参数字号与占位提示

信号源基础及高级参数的数值字体从 20 px / 600 改为 **16 px / 400**，空白提示改为 **13 px / 400**，灰色 `#879296`；单位仍为 13 px。输入框与单位框均保持 33 个逻辑像素高，沿用原有内边距、布局和深色琥珀配色。其余页面没有整体缩放。

占位文字不再使用“例如”或固定示例数字，改为“请输入频率”“请输入功率”“请输入扫频起始频率”“请输入扫描驻留时间”等。公共 `ParameterEditor` 新增可选关键字 `label`，单值操作复用操作名称，多参数操作复用字段名称；旧调用仍有效，未指定名称时按类型回退为“请输入数值 / 整数 / 内容”。没有新增型号分支或修改设备能力。

输入状态仅在空白/非空切换时刷新主题，程序读回和屏蔽信号的草稿恢复也同步状态；不替换输入文本，不修改光标和撤销历史。新增回归覆盖输入、清空、撤销、零值读回、草稿恢复、无名称回退及单/多参数名称。既有单位换算、范围限制和下拉菜单测试继续通过。

本轮完整软件回归 **504 项通过、1 项实机测试跳过**；原生 Windows 在最终 DPR 1.0 / 1.25 / 1.5 下各通过 **22 项**字号及参数菜单检查。Ruff、变更文件格式、mypy、生成文档一致性和 Markdown 检查通过；此前已记录的全仓既有格式问题未扩大修改。

`scripts/review_parameter_typography.py` 以正式 `RealWindow` 和临时 Mock 库存捕获 SMA100B、AFG31252、SDG2082X 的未连接、读回、正在编辑三种状态。每个缩放比例记录 9 张截图、66 个可见数值框状态；均验证占位名称、字号/字重、完整文字宽度、33 px 高度及单位对齐，参数写入计数为 0。SMA100B、AFG31252 同时检查扫频数值；SDG2082X 当前高级页没有数值输入项，保留其现有页面，不人为增加能力。

| 对照 | 截图 |
| --- | --- |
| 修改前，100%，空白提示 | [原 20 px 半粗体示例](../../design/reviews/parameter-typography/before-100/rs.sma100b-disconnected-sweep.png) |
| 修改后，100%，空白提示 | [请输入参数名](../../design/reviews/parameter-typography/after-100/rs.sma100b-disconnected.png) |
| 修改后，125%，空白提示 | [125% 字号层级](../../design/reviews/parameter-typography/after-125/rs.sma100b-disconnected.png) |
| 修改后，150%，空白提示 | [150% 字号层级](../../design/reviews/parameter-typography/after-150/rs.sma100b-disconnected.png) |
| AFG31252，基础与扫频读回 | [16 px 常规数值](../../design/reviews/parameter-typography/after-100/tek.afg31252-readback.png) |
| SDG2082X，正在编辑 | [现有参数页](../../design/reviews/parameter-typography/after-100/siglent.sdg2082x-editing.png) |

其余截图和 `metrics.json` 位于同级批次目录；[verification.json](../../design/reviews/parameter-typography/verification.json) 保存检查汇总、来源文件与截图哈希。源码中不保存现场设备身份或通信记录。原始软件测试日志在本地 `.tools/parameter-typography/`。以上是自动化事件与代理截图目视检查，没有进行实体鼠标/触摸板人工验收或真实设备测试。

复现字体截图时，同样应以当前机器实际原生 DPR 换算子进程缩放，并传入预期最终 DPR 检查：

```powershell
$env:QT_QPA_PLATFORM='windows'
$env:QT_SCALE_FACTOR='0.6666666666666666'  # 本机原生 DPR 为 1.5，最终为 1.0
.venv-dev/Scripts/python.exe scripts/review_parameter_typography.py .tools/parameter-typography-review --expect-dpr 1
```

正式 LabControl 进程需正常重启后加载更新，本轮未替用户重启或连接仪器。
