# 安装、接设备与实机验收

**项目强制规范：每台设备每次测试均须留档，包括失败或受阻。** 按[实机测试记录规范](hardware-test-records.md)保存初始状态、逐项结果、恢复与最终输出状态，并更新运行目录 `hardware-tests/index.md`。连接、参数控制与实际输出验收分别判定。

本轮交付的是可实测的软件。所有型号和能力的硬件验证状态仍为待验收；软件测试、连接成功、配置写入成功都不能代替物理结果核对。

各独立型号的测试入口及已提供的完整控制验证流程见[信号源型号导航](devices/signal-sources/README.md)。只读复核与输出关闭的控制验证分开执行；逐台实际结果保存在运行目录，不能从型号清单推断某台设备或某个接口已验收。

## 1. Windows 环境

使用 Python 3.12 x64。安装步骤和入口见[根 README](../README.md)。Python 依赖由 uv.lock 固定，厂商运行库独立安装：

| 接入 | 前置条件 | 排查入口 |
| --- | --- | --- |
| USBTMC / VISA LAN | 一套可用的 64 位 NI-VISA、TekVISA 或厂商 VISA | VISA 资源浏览器、设备管理器 |
| GPIB | 匹配控制器的驱动及 VISA | 控制器地址与仪器 GPIB 地址；3458A 使用原生命令 |
| COM | 厂家虚拟串口或 USB-串口驱动 | 核对 COM、波特率、8N1、终止符，不自动遍历 |
| 普通 TCP | 厂家 Socket 功能及准确端口 | IP/掩码、防火墙、厂家网络设置 |
| LeCroy VICP | 设备允许 VICP，通常 1861 | 使用 vicp transport，不使用普通 tcp 替代 |
| Pico | PicoSDK、ps6000a.dll、USB 驱动 x64 | 厂家示例能枚举和打开准确序列号 |
| Thorlabs | OPM、匹配的 TLPMX/旧 PM100D DLL 和驱动 | Driver Switcher 当前选择、位数、探头身份 |
| Microchip 53100A 候选 | 已配置的 TimeLab/TSERVE 服务 | 现场确认仪器/服务控制权，命令 1299、数据 1298 |

DLL 使用绝对路径和厂家安装目录；应用不下载、切换驱动或复位设备。[Thorlabs 驱动迁移说明](https://www.thorlabs.com/software/MUC/OPM/v5.0/TL_OPM_V5.2.pdf)说明旧版驱动与新驱动的差异，不能只替换 DLL 文件名。32 位库不能由本版 x64 进程加载；尚未提供 32 位代理进程。

VISA/厂商程序确认能识别后，释放其仪器会话再连接本软件。本软件真实 GUI、CLI、Python 示例之间有进程锁；真实 CLI 测试前退出真实 GUI。不会擅自关闭厂家软件或其他进程。

## 2. 盘点与配置

保存铭牌、背板、完整型号、序列号、固件、接口选件、驱动/SDK 版本、位数、设备管理器名称、资源、COM 参数、现有输出状态和接线说明。资料和报告放在运行数据目录，不提交到源码。

从 [config/examples](../config/examples/README.md) 复制一种模板，修改为现场值。实际配置可放 `%LOCALAPPDATA%\TF-LabControl\config\bench.toml`。示例中的 192.0.2.x 是保留地址，必须替换。PSW160、DS1202Z 原始后缀等信息不足条目保持待补。

常用环境检查：

```powershell
uv run lab-control-cli doctor
uv run lab-control-cli models
uv run lab-control-cli --config "$env:LOCALAPPDATA\TF-LabControl\config\bench.toml" scan
```

扫描不自动连接；SDK 只枚举。已有连接扫描使用缓存；新地址可能是活动设备别名时会延后身份探测。显示 unknown、driver error、probe timeout 或 deferred 均应保留并说明原因。

## 3. 单设备只读验收

下面变量必须填写实际配置和完整资源：

```powershell
$cfg = "$env:LOCALAPPDATA\TF-LabControl\config\bench.toml"
$resource = "TCPIP0::192.0.2.10::inst0::INSTR"
uv run lab-control-cli --config $cfg --resource $resource identity
uv run lab-control-cli --config $cfg --resource $resource capabilities
uv run lab-control-cli --config $cfg --resource $resource status
uv run lab-control-cli --config $cfg --resource $resource test
```

预期：厂家、型号、序列号/可信度与面板一致；未发送复位、输出或采集启动命令；报告路径在控制台显示。无序列号（例如 3458A）保持型号级身份。53100A 的型号/序列号仍依赖现场确认及 TSERVE 信息，不能用自定义服务标题伪装自动硬件确认。

每次真实进程退出会释放软件连接，但不宣称关闭输出。只读 test 保存身份、能力、状态及硬件未验收标记。失败的 test/set/acquire/stream 也保存错误报告。

## 4. 设置与输出测试

先检查负载、输出状态和安全范围，选择该型号能力中一项保守值。默认 set 只显示目标；实际写入需要 --write，危险动作还需要 --confirm。

```powershell
uv run lab-control-cli --config $cfg --resource $resource set frequency --parameters config/examples/frequency.json --write
uv run lab-control-cli --config $cfg --resource $resource set output --parameters config/examples/output-off.json --write --confirm
```

output 示例适用于有该能力的型号；GPD 整机使用 all_outputs。输出开启需自行提供 UTF-8 JSON `{"value": true}` 并核对目标和接线。幅度使用 Vpp；AFG/DG 先显式设置 amplitude_unit 为 VPP。设置已经开启的输出也会改变信号，不能只对“打开输出”做物理核对。

报告检查 requested/readback/completed_steps/outcome。出现 rejected/partial/uncertain 后停止该测试，核对仪器面板和错误，不重试整批或自动恢复旧值。离线重连不会恢复输出、列表或预设。

## 5. 各类别基础与扩展测试

| 类别 / 型号 | 基础步骤与预期 | 扩展测试要点 |
| --- | --- | --- |
| AFG31252 / DG5504 Pro / SDG6032X | 频率、幅度/偏置、相位、输出读回；外接示波器验证 | 上传后单独选择波形；AFG 大端 14 位、SDG 小端 signed16、DG 分块 .arb；调制/扫频/突发逐项开启并检查 |
| SMA100B | 频率、dBm 电平、RF 输出 | AM/FM/PM、频率/功率列表；核实频率/脉冲选件；列表上传不启动 |
| GPD / 2231A / SPD / IT6322 | 先限流、低电压，读设定/测量/输出；逐通道核对 | 全局/独立输出不能混淆；SPD 远端采样仅 CH2/3；列表先停止、清除再替换 |
| RTM / RTO / WaveRunner / DS1202Z-E | 前面板单次或停止后读取通道波形，比较时间轴和幅值 | 量程/耦合/探头倍率、触发、平均/高分辨率；双值峰值/分段未实现的导出应明确拒绝 |
| Pico 6404E | 显式配置通道，block 捕获并与厂家示例比较 | streaming 批次、多通道、超量程；检查 Stop/关闭后可以被厂家软件重新打开 |
| DSA815 / FPH | 起止频率、RBW/VBW、参考电平、单次轨迹 | marker/peak、零跨度秒轴；单位从设备读回，不强写为 dBm |
| PM100D + S154C | 确认仪表和探头，设置探头允许波长，再读功率 | 自动量程、平均、W/dBm；遮光后归零；拔探头应 sensor_missing，不能返回零功率 |
| 3458A | ID?、FUNC?、NPLC?；已知输入读数/单位 | 内存 RMEM 读取需确认（会停止存储），initiate 配置 TARM/NRDGS；逐行接收多读数 |
| ZNB3004 | 扫频、点数、IF 带宽、复数轨迹、频率轴 | s_parameter 创建/选择轨迹；保存修正状态；S11/S21/S12/S22 同一频率网格才能导出 .s2p |
| 53100A 候选 | show version/state/inputs；读取已运行测量的相噪/AM/ADEV | set 频率公共 Hz 转 MHz；P/F 数据口只收不发，必须匹配 /timestamp:0 与实际采样周期 |
| 待补型号类别 | 只验证库存、模拟接口和未支持提示 | 取得协议后再编写真实测试，不向未知设备试命令 |

波形与批次采集：

```powershell
uv run lab-control-cli --config $cfg --resource $resource acquire --kind waveform --points 1000
uv run lab-control-cli --config $cfg --resource $resource acquire --kind spectrum --initiate --confirm
uv run lab-control-cli --config $cfg --resource $resource stream --kind scalar --count 10 --interval 0.5
```

按目标类型选择其中一条，不把 spectrum 请求发给示波器。Pico 使用 [examples/pico.py](../examples/pico.py) 先配置通道。CLI 每次独立连接，SDK 中仅缓存的通道配置不能依赖上一次 CLI 调用；需要多步骤在同一个 Python 示例会话完成。

台式示波器读取的是当前整条波形，points 不保证裁成该点数；DS1202Z-E 当前导出 NORM 屏幕点。多通道按顺序读取，运行状态可能跨帧，应先完成单次采集。应用 repeated stream 与 Pico/TSERVE 批次间均可能有空隙，本版不承诺无间隙无限流或硬件同步时间戳。

## 6. 多设备扫描与故障验收

同机单进程执行，显式列出参与的资源：

```powershell
uv run lab-control-cli --config $cfg scan-stress --resources "资源A" "资源B" --repeats 5 --capture-resource "资源A" --kind waveform --points 1000
```

根据仪器需求另加 --initiate --confirm。该工具连接指定设备，保存扫描前后 sessionId、逐项结果和采集路径；单台连接失败单独记录，其他设备继续检查。报告的 software_checks_passed 仍不代表现场完成所有物理验收。

现场扩大到 6–20 台，至少各覆盖一次 VISA USB、LAN、GPIB、COM、SDK。确认扫描前后原句柄/会话保持，采集不断开；反复扫描和取消，不新增控制查询。GUI 中保持选中设备和滚动位置，切换模式只改变适用性。

分别模拟未连接设备拔出、已连接设备拔出/插回、驱动缺失、端口占用、错误地址、身份超时、SDK 缺失/位数不匹配。发现状态与会话错误应独立变化；历史身份和有效数据不丢失。USB+LAN 双接时先扫描识别合并，验证只有一条物理库存和两个候选资源；身份冲突保留诊断，不自动覆盖资产。

## 7. 数据与失败反馈

CLI acquire/stream、GUI 显式记录或保存会生成 snapshot.json、UTF-8 BOM samples.csv 和 samples.npz；GUI 纯监测不生成实验数据文件。ZNB 示例另存 Touchstone。报告位于 reports，日志位于 logs。使用 LAB_CONTROL_HOME 可改根目录；模拟数据另存 simulation。超时、断线、传感器缺失和解析错误使用质量标记/空数组，不以零值补齐。

现场记录模板可复制 [config/examples/hardware-result.json](../config/examples/hardware-result.json)。反馈时提供：报告、对应日志片段、配置（删除不希望分享的地址/序列号）、完整型号/固件/驱动、触发步骤、预期和实际读回、波形/频谱文件及仪器面板对比。不要把“success”直接改成全设备已验收。

## 8. 显式 pytest 硬件入口

只读测试默认跳过。退出真实 GUI 后，指定准确目标才会执行：

```powershell
$env:LAB_CONTROL_HARDWARE_CONFIG = $cfg
$env:LAB_CONTROL_HARDWARE_RESOURCE = $resource
uv run pytest tests/hardware -m hardware -v
Remove-Item Env:LAB_CONTROL_HARDWARE_CONFIG
Remove-Item Env:LAB_CONTROL_HARDWARE_RESOURCE
```

只读硬件测试使用独立临时目录，不覆盖日常库存。需要保留长期验收材料时优先 CLI test。每个型号完成身份、基础控制、状态、采集、单位/时轴和故障恢复后，才在现场验收记录中确认“可控”；高级功能逐项记录，不能自动沿用基础验收结论。

## 9. 0.3.0 页面现场验收

按[页面指南](control-and-monitoring.md)操作，先单设备，再扩展至 6–20 台。软件自动化和离屏检查只覆盖 Mock，不替代以下核对。

| 场景 | 操作 | 应保留的证据 |
| --- | --- | --- |
| 分类调参 | 核对设备/槽位/物理通道，输入带单位值；先取消确认，再明确执行 | 面板与请求/读回对应；取消无输出变化；整机与通道操作未混淆 |
| 草稿与 SDK 来源 | 编辑未应用值，读取现值、切页再返回 | 草稿保留；仪器读回、本会话缓存和未知来源明确 |
| 上传/列表 | 导入小规模 UTF-8 文件，预览/范围检查，按型号步骤上传 | 导入点数、文件、设备状态、面板数据；上传不自动打开输出 |
| 多通道与坐标 | 示波器接已知信号，频谱选择普通/零跨度，网络选择 S 参数 | 电压/时间/频率轴、dB/相位及单位与仪器一致；实际点数记录 |
| 实时调参 | 监测期间修改一个影响曲线的参数；返回总览、扫描并缩放窗口 | 日志顺序为完成读取→设置/读回→新批次；sessionId 不变，其他仪器继续 |
| 监测与记录 | 仅监测后查看 data；显式记录、停止记录、保存当前数据 | 纯监测无实验文件；记录和保存有原始 NPZ/CSV/快照；写盘结束后可切模式 |
| 失败恢复 | 目标设备超时/拔线，观察最后画面、错误及其他设备 | 仅目标暂停、质量/错误可解释，不补零；手动处理并重连，不恢复输出 |
| 总览布局 | 同源多卡片，标题定位通道，单列至四列窗口，模式切换 | 不额外打开会话；卡片重排不重启采集；模式布局互不覆盖 |

停止软件监测后，另行核对仪器仍处于原输出/运行状态；确需停止仪器或关输出时使用对应独立动作。报告继续标记具体能力待验收，不能将软件检查通过写成物理验证通过。
