# 安装、接设备与实机验收

**项目强制规范：每台设备每次测试均须留档，包括失败或受阻。** 按[实机测试记录规范](records.md)保存初始状态、逐项结果、恢复与最终输出状态，并更新运行目录 `hardware-tests/index.md`。连接、参数控制与实际输出验收分别判定。

现有部分资产已有通信和输出关闭下的控制证据；每项验收仍须核对对应资产、接口、固件和测试范围。软件实现与物理性能验收分别判定。

各独立型号的测试入口及已提供的完整控制验证流程见[型号导航](../devices/README.md)。只读复核与输出关闭的控制验证分开执行；逐台实际结果保存在运行目录，不能从型号清单推断某台设备或某个接口已验收。

## 1. Windows 环境

使用 Python 3.12 x64。安装步骤和入口见[根 README](../../README.md)。Python 依赖由 uv.lock 固定，厂商运行库独立安装：

| 接入 | 前置条件 | 排查入口 |
| --- | --- | --- |
| USBTMC / VISA LAN | 一套可用的 64 位 NI-VISA、TekVISA 或厂商 VISA | VISA 资源浏览器、设备管理器 |
| GPIB | 匹配控制器的驱动及 VISA | 控制器地址与仪器 GPIB 地址；3458A 使用原生命令 |
| COM | 厂家虚拟串口或 USB-串口驱动 | 核对 COM、波特率、8N1、终止符，不自动遍历 |
| 普通 TCP | 厂家 Socket 功能及准确端口 | IP/掩码、防火墙、厂家网络设置 |
| LeCroy VICP | 设备允许 VICP，通常 1861 | 使用 vicp transport，不使用普通 tcp 替代 |
| Pico | PicoSDK、ps6000a.dll、USB 驱动 x64 | 厂家示例能枚举和打开准确序列号 |
| Thorlabs | OPM、匹配的 TLPMX/旧 PM100D DLL 和驱动 | Driver Switcher 当前选择、位数、探头身份 |
| Microchip 53100A | TimeLab 驱动 + 托管本机 TSERVE64，或已有外部服务 | USB 扫描仅枚举；本机连接实际读回 USB 身份；服务配置、直接硬件身份与测量分开留档，见[型号计划](../../src/lab_control/instruments/devices/microchip_53100a/hardware/README.md) |

DLL 使用绝对路径和厂家安装目录；应用不下载、切换驱动或复位设备。[Thorlabs 驱动迁移说明](https://www.thorlabs.com/software/MUC/OPM/v5.0/TL_OPM_V5.2.pdf)说明旧版驱动与新驱动的差异，不能只替换 DLL 文件名。32 位库不能由本版 x64 进程加载；尚未提供 32 位代理进程。

VISA/厂商程序确认能识别后，释放其仪器会话再连接本软件。本软件真实 GUI、CLI、Python 示例之间有进程锁；真实 CLI 测试前退出真实 GUI。不会擅自关闭厂家软件或其他进程。

## 2. 盘点与配置

保存铭牌、背板、完整型号、序列号、固件、接口选件、驱动/SDK 版本、位数、设备管理器名称、资源、COM 参数、现有输出状态和接线说明。资料和报告放在运行数据目录，不提交到源码。

从 [config/examples](../../config/examples/README.md) 复制一种模板，修改为现场值。实际配置可放 `%LOCALAPPDATA%\TF-LabControl\config\bench.toml`。示例中的 192.0.2.x 是保留地址，必须替换。PSW160、DS1202Z 原始后缀等信息不足条目保持待补。

常用环境检查：

```powershell
.venv-dev\Scripts\lab-control-cli.exe doctor
.venv-dev\Scripts\lab-control-cli.exe models
.venv-dev\Scripts\lab-control-cli.exe --config "$env:LOCALAPPDATA\TF-LabControl\config\bench.toml" scan
```

扫描不自动连接；SDK 只枚举。已有连接扫描使用缓存；新地址可能是活动设备别名时会延后身份探测。显示 unknown、driver error、probe timeout 或 deferred 均应保留并说明原因。

## 3. 单设备只读验收

下面变量必须填写实际配置和完整资源：

```powershell
$cfg = "$env:LOCALAPPDATA\TF-LabControl\config\bench.toml"
$resource = "TCPIP0::192.0.2.10::inst0::INSTR"
.venv-dev\Scripts\lab-control-cli.exe --config $cfg --resource $resource identity
.venv-dev\Scripts\lab-control-cli.exe --config $cfg --resource $resource capabilities
.venv-dev\Scripts\lab-control-cli.exe --config $cfg --resource $resource status
.venv-dev\Scripts\lab-control-cli.exe --config $cfg --resource $resource test
```

预期：厂家、型号、序列号/可信度与面板一致；未发送复位、输出或采集启动命令；报告路径在控制台显示。无序列号（例如 3458A）保持型号级身份。53100A 本机空闲连接使用独立 USB 描述符读回，外部 TSERVE 要求 show serial；不能用自定义服务标题伪装硬件确认。

每次真实进程退出会释放软件连接，但不宣称关闭输出。只读 test 保存身份、能力、状态及硬件未验收标记。失败的 test/set/acquire/stream 也保存错误报告。

## 4. 设置与输出测试

先检查负载、输出状态和安全范围，选择该型号能力中一项保守值。默认 set 只显示目标；实际写入需要 --write，危险动作还需要 --confirm。

```powershell
.venv-dev\Scripts\lab-control-cli.exe --config $cfg --resource $resource set frequency --parameters config/examples/frequency.json --write
.venv-dev\Scripts\lab-control-cli.exe --config $cfg --resource $resource set output --parameters config/examples/output-off.json --write --confirm
```

output 示例适用于有该能力的型号；GPD 整机使用 all_outputs。输出开启需自行提供 UTF-8 JSON `{"value": true}` 并核对目标和接线。幅度使用 Vpp；AFG/DG 先显式设置 amplitude_unit 为 VPP。设置已经开启的输出也会改变信号，不能只对“打开输出”做物理核对。

报告检查 requested/readback/completed_steps/outcome。出现 rejected/partial/uncertain 后停止该测试，核对仪器面板和错误，不重试整批或自动恢复旧值。离线重连不会恢复输出、列表或预设。

## 5. 各类别基础与扩展测试

| 类别 | 通用验证要求 |
| --- | --- |
| 信号源与电源 | 参数设定/读回、输出状态、通道隔离、范围、模式关联与恢复；物理输出单独测量 |
| 示波器与频谱类 | 数据点数、单位、坐标轴、触发/采集状态、通道与配置一致性 |
| 光学与测量类 | 主机/探头身份、量程与单位、有效质量标记、恢复与会话释放 |
| 身份或协议待补 | 只做明确授权的身份/资料确认，不能尝试猜测命令 |

型号专属命令、备份恢复和限制见[型号导航](../devices/README.md)及对应包内硬件说明。先按[新设备流程](onboarding.md)确定本次矩阵，不能将一种型号的流程用于另一种型号。

波形与批次采集：

```powershell
.venv-dev\Scripts\lab-control-cli.exe --config $cfg --resource $resource acquire --kind waveform --points 1000
.venv-dev\Scripts\lab-control-cli.exe --config $cfg --resource $resource acquire --kind spectrum --initiate --confirm
.venv-dev\Scripts\lab-control-cli.exe --config $cfg --resource $resource stream --kind scalar --count 10 --interval 0.5
```

按目标类型选择其中一条，不把 spectrum 请求发给示波器。Pico 使用 [examples/pico.py](../../examples/pico.py) 先配置通道。CLI 每次独立连接，SDK 中仅缓存的通道配置不能依赖上一次 CLI 调用；需要多步骤在同一个 Python 示例会话完成。

台式示波器读取的是当前整条波形，points 不保证裁成该点数；DS1202Z-E 当前导出 NORM 屏幕点。多通道按顺序读取，运行状态可能跨帧，应先完成单次采集。应用 repeated stream 与 Pico/TSERVE 批次间均可能有空隙，本版不承诺无间隙无限流或硬件同步时间戳。

## 6. 多设备扫描与故障验收

同机单进程执行，显式列出参与的资源：

```powershell
.venv-dev\Scripts\lab-control-cli.exe --config $cfg scan-stress --resources "资源A" "资源B" --repeats 5 --capture-resource "资源A" --kind waveform --points 1000
```

根据仪器需求另加 --initiate --confirm。该工具连接指定设备，保存扫描前后 sessionId、逐项结果和采集路径；单台连接失败单独记录，其他设备继续检查。报告的 software_checks_passed 仍不代表现场完成所有物理验收。

现场扩大到 6–20 台，至少各覆盖一次 VISA USB、LAN、GPIB、COM、SDK。确认扫描前后原句柄/会话保持，采集不断开；反复扫描和取消，不新增控制查询。GUI 中保持选中设备和滚动位置，切换页面不重建会话或停止采集。当前界面使用通用工作区，不提供旧电子／光子模式切换。

分别模拟未连接设备拔出、已连接设备拔出/插回、驱动缺失、端口占用、错误地址、身份超时、SDK 缺失/位数不匹配。发现状态与会话错误应独立变化；历史身份和有效数据不丢失。USB+LAN 双接时先扫描识别合并，验证只有一条物理库存和两个候选资源；身份冲突保留诊断，不自动覆盖资产。

## 7. 数据与失败反馈

CLI `acquire` 与 GUI 保存当前帧在本批目录的 `_raw` 中保存 `snapshot.json` 和 `samples.npz`；CLI `stream` 与 GUI 连续记录使用 `_raw/segment-*.sqlite3` 保存逐帧原始数据，不逐帧生成 NPZ。新目录顶层提供 `记录说明.txt`、`数据_00001.csv` 等分析文件，连续曲线另有 `帧索引.csv`；旧版 `samples.csv` 布局仍可读取。完整目录与恢复方法见[文件与反馈](../guides/control-and-monitoring.md#文件与反馈)和[数据模型](../data/data-model.md#实际存储结构)。

GUI 纯监测不生成实验数据文件，显式记录和保存才写盘。ZNB 示例另存 Touchstone。报告位于 `reports`，日志位于 `logs`；使用 `LAB_CONTROL_HOME` 可改运行根目录，模拟数据另存 `simulation`。数据保存一级目录可在软件设置中指定。超时、断线、传感器缺失和解析错误保留诊断；无效帧不得显示或保存，不以零值补齐。

现场记录模板可复制 [config/examples/hardware-result.json](../../config/examples/hardware-result.json)。反馈时提供：报告、对应日志片段、配置（删除不希望分享的地址/序列号）、完整型号/固件/驱动、触发步骤、预期和实际读回、波形/频谱文件及仪器面板对比。不要把“success”直接改成全设备已验收。

## 8. 显式 pytest 硬件入口

本节保留旧锚点。顶层 `tests/hardware/test_readonly.py` 已退役并无条件跳过，即使残留 `LAB_CONTROL_HARDWARE_CONFIG` / `LAB_CONTROL_HARDWARE_RESOURCE` 也不会连接设备。普通软件检查不提供启用真实硬件的开关。

现场使用[型号维护源](../devices/README.md)中对应包的 `hardware/README.md` 和脚本，核对完整型号、序列号、正式运行目录、授权范围及进程锁；按[记录规范](records.md)保存通过、失败、跳过或停止的结果。只读预检必须跳过会修改格式的连接初始化。每个型号完成身份、基础控制、状态、采集、单位/时轴和故障恢复后，才在现场验收记录中确认“可控”；高级功能逐项记录，不能自动沿用基础验收结论。

## 9. 0.3.0 页面现场验收

按[页面指南](../guides/control-and-monitoring.md)操作，先单设备，再扩展至 6–20 台。软件自动化和离屏检查只覆盖 Mock，不替代以下核对。

| 场景 | 操作 | 应保留的证据 |
| --- | --- | --- |
| 分类调参 | 核对设备/槽位/物理通道，输入带单位值；先取消确认，再明确执行 | 面板与请求/读回对应；取消无输出变化；整机与通道操作未混淆 |
| 草稿与 SDK 来源 | 编辑未应用值，读取现值、切页再返回 | 草稿保留；仪器读回、本会话缓存和未知来源明确 |
| 多通道与坐标 | 示波器接已知信号，频谱选择普通/零跨度，网络选择 S 参数 | 电压/时间/频率轴、dB/相位及单位与仪器一致；实际点数记录 |
| 实时调参 | 监测期间修改一个影响曲线的参数；返回总览、扫描并缩放窗口 | 日志顺序为完成读取→设置/读回→新批次；sessionId 不变，其他仪器继续 |
| 监测与记录 | 仅监测后查看实际保存目录；显式记录、停止记录、保存当前帧 | 纯监测无实验文件；单帧有快照/NPZ，连续记录有 SQLite；二者均有分析 CSV；停止后二进制收尾释放参数锁，CSV 可继续后台转换 |
| 失败恢复 | 目标设备超时/拔线，观察最后画面、错误及其他设备 | 仅目标暂停、质量/错误可解释，不补零；手动处理并重连，不恢复输出 |
| 总览布局 | 核对每台已连接设备的卡片、参与采样选择与共享采样间隔；调整窗口宽度、切换设备页并返回 | 卡片自动排列；布局变化不额外打开会话或重启采集；切页后保留任务与最后有效帧 |

上传、列表、调制等高级功能仅按型号已声明的 CLI／底层接口另行验收；当前简洁信号源页面不提供这些高级分组，不能将其列为 GUI 操作。

停止软件监测后，另行核对仪器仍处于原输出/运行状态；确需停止仪器或关输出时使用对应独立动作。报告继续标记具体能力待验收，不能将软件检查通过写成物理验证通过。
