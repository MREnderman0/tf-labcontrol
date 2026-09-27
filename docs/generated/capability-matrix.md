# 实际能力矩阵（自动生成）

由 `scripts/generate_device_docs.py` 从实际注册表生成。下面的操作均有实现；单项参数、命令定位与软件测试标记见 [JSON](capability-matrix.json)。未列出的功能返回 `unsupported`；选件或 TSERVE 配置缺失会禁用相应能力。具体资产的实机证据保存在运行目录，不将个别资产的验证结果推广到整个型号。

软件测试分为通用参数约束、特定协议夹具、SDK 生命周期和应用集成；协议夹具通过不代表目标固件通过，也不代表所有高级组合已经验证。

JSON 保留 hardware_tested/hardware_passed 兼容字段；固定 false 不代表现场设备失败或全部未测。资产证据见[离线台账说明](../testing/records.md)。

| 型号 | 注册 ID | 操作数 | 采集 | 详情 |
| --- | --- | --- | --- | --- |
| RIGOL DG5504 Pro | `rigol.dg5504pro` | 41 | 外部仪器测量输出 | [型号卡片](../devices/signal-sources/dg5504-pro.md) |
| Tektronix AFG31252 | `tek.afg31252` | 37 | 外部仪器测量输出 | [型号卡片](../devices/signal-sources/afg31252.md) |
| Siglent SDG2082X | `siglent.sdg2082x` | 42 | 外部仪器测量输出 | [型号卡片](../devices/signal-sources/sdg2082x.md) |
| R&S SMA100B | `rs.sma100b` | 33 | 外部仪器测量输出 | [型号卡片](../devices/signal-sources/sma100b.md) |
| GW Instek GPD-4303S | `gwinstek.gpd4303s` | 10 | power | [型号卡片](../devices/power-supplies/gpd-4303s.md) |
| RIGOL DSA815 | `rigol.dsa815` | 45 | dsa | [型号卡片](../devices/spectrum-analyzers/rigol-dsa815.md) |
| Teledyne LeCroy WaveRunner 9404M | `lecroy.9404m` | 23 | lecroy | [型号卡片](../devices/oscilloscopes/waverunner-9404m.md) |
| RIGOL DHO924 | `rigol.dho924` | 36 | dho924 | [型号卡片](../devices/oscilloscopes/rigol-dho924.md) |
| R&S FPH02 | `rs.fph` | 35 | fph | [型号卡片](../devices/spectrum-analyzers/rs-fph.md) |
| Siglent SDG6032X | `siglent.sdg6032x` | 36 | 外部仪器测量输出 | [型号卡片](../devices/signal-sources/sdg6032x.md) |
| Keithley 2231A-30-3 | `keithley.2231a` | 15 | power | [型号卡片](../devices/power-supplies/keithley-2231a.md) |
| Siglent SPD4306X | `siglent.spd4306x` | 22 | power | [型号卡片](../devices/power-supplies/spd4306x.md) |
| ITECH IT6322 | `itech.it6322` | 8 | power | [型号卡片](../devices/power-supplies/it6322.md) |
| PicoScope 6404E | `pico.6404e` | 6 | block/streaming | [型号卡片](../devices/oscilloscopes/picoscope-6404e.md) |
| R&S RTM3004 | `rs.rtm3004` | 21 | rtm | [型号卡片](../devices/oscilloscopes/rtm3004.md) |
| R&S RTO64 | `rs.rto64` | 16 | rto | [型号卡片](../devices/oscilloscopes/rto64.md) |
| Teledyne LeCroy WaveRunner 9254 | `lecroy.9254` | 14 | lecroy | [型号卡片](../devices/oscilloscopes/waverunner-9254.md) |
| RIGOL DS1202Z（待确认后缀）/ DS1202Z-E | `rigol.ds1202ze` | 21 | rigol_scope | [型号卡片](../devices/oscilloscopes/rigol-ds1202z.md) |
| Thorlabs PM100D + S154C | `thorlabs.pm100d` | 8 | scalar | [型号卡片](../devices/measurement-analyzers/thorlabs-pm100d-s154c.md) |
| Keysight / Agilent / HP 3458A | `keysight.3458a` | 16 | dmm | [型号卡片](../devices/measurement-analyzers/keysight-3458a.md) |
| R&S ZNB3004 | `rs.znb3004` | 14 | vna | [型号卡片](../devices/measurement-analyzers/rs-znb3004.md) |
| 53100A（Microchip 候选） | `microchip.53100a` | 8 | phase_noise/am_noise/stability/streaming | [型号卡片](../devices/measurement-analyzers/phase-noise-53100a.md) |

## 未实现 / 待补项

通用协议错误处理与已注册功能可进入实测。下列功能尚未作为真实能力开放，调用返回 `unsupported`，不能仅靠配置开启：

- 函数发生器复杂序列编辑；SMA100B 未注册的选件专用功能（频率、扫频边界和功率范围已按实机查询适配）。
- 电源斜率、触发链以及未核实型号的远端采样/列表（SPD4306X 已实现列表与 CH2/3 远端采样）。
- 示波器分段逐段时间戳、多帧/数学波形导出；Pico 无间隙无限 streaming。
- 频谱仪前置放大与带宽/信道功率应用；网络分析仪分段表写入及完整校准向导。
- PM100D 校准证书导出；未知厂家设备真实协议。

这些是后续能力扩展任务，不标记为硬件不具备。

| 原始条目 | 状态 |
| --- | --- |
| PSW160 | 待补信息：Manufacturer and complete suffix required |
| DS1202Z | 待补信息：Confirm exact suffix; DS1202Z-E adapter is deliberately not an alias |
| GOCA-8-NK2603212 | 待补信息：Manufacturer/protocol required |
| multi-channel optical power meter | 待补信息：Model and protocol required |
| temperature_controller | 待补信息：Model and protocol required |
| electronic_load | 待补信息：Model and protocol required |
| pump_driver | 待补信息：Model, protocol and interlocks required |
| optical_spectrometer | 待补信息：Model and protocol required |
