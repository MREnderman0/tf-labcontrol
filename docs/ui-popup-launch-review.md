# 下拉菜单、首次显示与启动入口验收

日期：2026-09-24。基于已确认的深色琥珀卡片和紧凑信号源页面，应用比例保持 100%。本轮仅作软件验收，没有扫描、连接、写入真实仪器或开启输出。测试与截图使用临时目录中的隔离 Mock 服务；截图中的型号默认范围不是现场设备的能力或验收结果。

## 修复与行为

- 单位和参数枚举共用 `ParameterComboBox`、`QListView` 和条目委托，每行 28 个逻辑像素，最多显示 10 行。弹出高度使用实际条目高度加弹出层留白；宽度容纳最长选项，屏幕边缘向上展开或按完整行滚动。
- 共用参数编辑器、按钮、读回和提示标签在设置可见性前已有所属父控件，避免首次建页产生短暂顶层窗口。主动输出确认仍保留。
- 根目录只保留 `run.bat`，使用项目 `.venv-dev` 运行 `python -m lab_control`。模块与 `lab-control` 统一调用 `app.main`，CLI 保留，移除演示注册项和旧启动器。Mock 仅用于内部测试和开发预览。
- 单位换算、最终单位限幅、非法输入静默恢复、草稿保存、应用按钮状态均沿用原规则；编辑和选择单位不下发命令。正式库存继续使用原路径，历史模拟数据保持隔离，不迁移、不删除、不自动填充新库存。

## 功能验证

`tests/integration/test_parameter_popups.py` 捕获冷启动后首次进入信号源、切换双通道/四通道波形源与射频源及高级页的所有顶层 Show 事件：未出现额外窗口。检查幅度 2 项、频率 4 项、时间 4 项、波形枚举及 20 项长菜单，逐项选择、完整行、最长文字宽度和屏幕边缘定位均通过。

输入回归包括先键入 500 再选 mVpp，最终为 0.5 Vpp，不按原单位提前限幅；草稿保存正确、应用按钮亮起、设备写入计数为零。既有回归覆盖 NaN/inf/空值恢复、上下界、读取不覆盖草稿、部分执行失败和输出独立确认。

启动验证覆盖带空格的配置参数、`run.bat --help`、已安装 GUI/CLI 启动器、缺环境时提示并调用安装脚本、安装失败退出码、空正式库存和已有库存/草稿的重启保留。所有库存测试均使用临时目录和临时进程锁，没有访问真实库存。已安装的 GUI 注册项仅为 `lab-control = lab_control.app:main`，旧 `lab-control-demo.exe` 已移除。

项目重新安装使用 `uv pip install --offline --no-deps ... -e .`；随后 `UV_OFFLINE=1` 下连续两次运行 `scripts/setup-dev.ps1` 成功。清华镜像配置、代理默认禁用和原锁定依赖保持，25 个已安装包依赖检查通过，本轮没有依赖下载。

## 独立视觉审查

使用 `scripts/review_parameter_popups.py` 直接捕获 Qt 页面和真实弹出控件，再按实际位置合成两块捕获画面。所有菜单截图单独保存在 [parameter-popups](../design/reviews/parameter-popups/)；各目录的 `metrics.json` 记录窗口、屏幕、设备像素比、菜单条目和视口大小。

| 场景 | 结果与截图 |
| --- | --- |
| 1440×900，SMA100B 频率 | [四项频率完整](../design/reviews/parameter-popups/rf-frequency-1440.png)，文字清晰、琥珀选中态正确 |
| 1100×760，双通道幅度 | [mVpp / Vpp 两行完整](../design/reviews/parameter-popups/amplitude-1100.png)，视口 56 逻辑像素，无裁切 |
| 1100×760，波形菜单 | [全部选项完整](../design/reviews/parameter-popups/waveform-1100.png)，与通道面板对齐 |
| 1440×900，高级时间单位 | [ns / µs / ms / s 四项](../design/reviews/parameter-popups/time-1440.png)，视口 112 逻辑像素 |
| 1024×600 工作区 | 外框适配可用区域，保留双列与展开侧栏，内容可纵向滚动 |
| 800×560 虚拟屏幕 | [底部时间菜单向上展开](../design/reviews/parameter-popups/low-resolution/time-800.png)，范围与操作可通过滚动访问 |
| Qt DPI 150%，1100×760 逻辑窗口 | [幅度菜单](../design/reviews/parameter-popups/dpi-150/amplitude-1100.png)，每行仍为 28 逻辑像素 / 42 物理像素，应用比例未改变 |

对齐、范围换行、行高、选中颜色和禁用应用按钮分别检查通过。菜单几何与选择测试还在 `QT_SCALE_FACTOR=1.25`、`1.5` 的独立进程各通过 14 项。DPI 使用 Qt 离屏平台模拟，没有修改 Windows 显示设置；真实多屏热插拔仍属于现场环境检查。

复现：

```powershell
.venv-dev\Scripts\python.exe scripts/review_parameter_popups.py .tools/popup-review
.venv-dev\Scripts\python.exe scripts/review_parameter_popups.py .tools/popup-low --screen 800 560 --window 800 560
$env:QT_SCALE_FACTOR='1.5'
.venv-dev\Scripts\python.exe scripts/review_parameter_popups.py .tools/popup-dpi --screen 2400 1500 --window 1100 760
```

## 回归结果

工作区软件回归 247 项通过、1 项实机测试跳过；提交范围另行导出后，183 项通过、1 项实机测试跳过，mypy、Ruff 检查与格式、生成文档一致性、Markdown 检查均通过。同步修正上一版信号源回归测试中一处集合推导式的格式。其他设备任务的未提交修改保留，不纳入本轮提交。

独立导出可离线构建 sdist 与 wheel；源码包只包含 `run.bat`，wheel 中 GUI 注册项只有 `lab-control`，CLI 保留。菜单组件、琥珀主题及审查截图与指标已列入打包清单。Windows 原生弹出交互未作自动化复验，本轮菜单尺寸、选项点击及 DPI 验证使用相同 Qt 组件的离屏测试。
