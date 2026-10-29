# 设备接入索引（0.3.0）

这是附件设备条目的唯一索引；具体能力由[实际矩阵](generated/capability-matrix.md)生成，型号卡片记录连接和验收。附件空白指令/载荷数据表、历史日期和疑似资产号不被当成已确认协议。

“已实现”表示存在真实传输与型号代码；“软件测试”不等于“实机通过”。具体资产及接口的验证结果以运行目录中的批次记录为准。

0.3.0 分类控制与实时监测已接入上述能力；每张型号卡片说明页面入口，能力 JSON 逐项记录中文标签和分组。使用方法见[控制与监测指南](guides/control-and-monitoring.md)。未知型号仍只有隔离模拟示例。

| 类别 | 附件设备 | 电脑交互方式 | 实际注册 ID / 状态 | 归档 |
| --- | --- | --- | --- | --- |
| 信号源 | SDG6032X | USB/LAN VXI-11/Socket | 原附件说明：待实机；现行状态见下方模块索引 | [模块索引](#独立型号模块自动生成) |
| 电源 | Keithley 2231A | 2231A-001 USB 适配器/COM | 原附件说明：仅精确匹配 2231A-30-3；现行状态见下方模块索引 | [模块索引](#独立型号模块自动生成) |
| 电源 | PSW160 | 待确认厂家、完整型号与接口 | 不推定为 EA 或其他厂家；不注册真实协议 | [待补卡片](devices/power-supplies/psw160.md) |
| 电源 | SPD4306X | USB/LAN VISA/Socket | 原附件说明：待实机；现行状态见下方模块索引 | [模块索引](#独立型号模块自动生成) |
| 电源 | IT6322 | RS-232/实际已安装 USB/GPIB | 原附件说明：独立旧型号协议；现行状态见下方模块索引 | [模块索引](#独立型号模块自动生成) |
| 示波器 | PicoScope 6404E | USB + ps6000a SDK | 原附件说明：待实机；现行状态见下方模块索引 | [模块索引](#独立型号模块自动生成) |
| 示波器 | RTM3004 | LAN/USB VISA/SCPI | 原附件说明：待实机；现行状态见下方模块索引 | [模块索引](#独立型号模块自动生成) |
| 示波器 | RTO64 | LAN/USB VISA/SCPI | 原附件说明：RTO6 手册，待实机；现行状态见下方模块索引 | [模块索引](#独立型号模块自动生成) |
| 示波器 | WaveRunner 9254 | LAN VICP/VXI-11/USBTMC | 原附件说明：待实机；现行状态见下方模块索引 | [模块索引](#独立型号模块自动生成) |
| 示波器 | DS1202Z | 原条目待确认后缀；DS1202Z-E 为 USB/LAN VISA | 原附件说明：不静默改写原型号；现行状态见下方模块索引 | [模块索引](#独立型号模块自动生成) |
| 光功率 | PM100D + S154C | 控制台 USB + TLPMX/旧 PM100D SDK；探头接控制台 | 原附件说明：待实机；现行状态见下方模块索引 | [模块索引](#独立型号模块自动生成) |
| 光功率 | 多通道光功率计 | 待完整型号与 SDK/协议 | 仅类别模拟 | [待补卡片](devices/measurement-analyzers/multi-channel-optical-power-meter.md) |
| 万用表 | Keysight 3458A | GPIB 控制器/VISA，原生非 SCPI 命令 | 原附件说明：待实机；现行状态见下方模块索引 | [模块索引](#独立型号模块自动生成) |
| 网络分析 | ZNB3004 | LAN/USB VISA/SCPI | 原附件说明：ZNB3000 手册，待实机；现行状态见下方模块索引 | [模块索引](#独立型号模块自动生成) |
| 待确认类别 | GOCA-8-NK2603212 | 待厂家和型号/资产号划分 | 不注册真实命令 | [待补卡片](devices/measurement-analyzers/goca-8.md) |
| 相噪仪 | 53100A | Microchip USB / TSERVE | 实现与连接路线见下方模块索引；实测按资产查证 | [模块索引](#独立型号模块自动生成) |
| 温控 | 商用温控仪 | 待型号、串口/以太网/SDK 依据 | 仅类别模拟 | [待补卡片](devices/temperature-load-drivers/temperature-controller.md) |
| 负载 | 电子负载 | 待型号和接口 | 仅类别模拟 | [待补卡片](devices/temperature-load-drivers/electronic-load.md) |
| 泵浦 | 地检泵浦驱动 | 待厂家协议、型号和联锁 | 仅类别模拟 | [待补卡片](devices/temperature-load-drivers/pump-driver.md) |
| 光谱 | 光谱仪 | 待型号与 SDK/SCPI 依据 | 仅类别模拟 | [待补卡片](devices/temperature-load-drivers/optical-spectrum-analyzer.md) |
| 基础设施 | 交换机、测试台式机 | 网络及运行环境盘点 | 不进入仪器兼容层 | 本索引 |
| 未确认标识 | MY54410307 | 疑似序列号或资产编号 | 原样保留，不建立型号适配器 | 本索引 |

## 独立型号模块（自动生成）

以下型号已从上述旧型号清单迁入独立目录，由注册元数据生成；附件清单之外新增的型号也在此索引。

<!-- device-modules:start -->
| 型号 | 注册 ID | 连接路线 | 归档 |
| --- | --- | --- | --- |
| PicoScope 6404E | `pico.6404e` | USB 3.x + ps6000a SDK | [卡片](devices/oscilloscopes/picoscope-6404e.md) |
| Thorlabs PM100D + S154C | `thorlabs.pm100d` | PM100D USB；S154C 接控制台传感器接口 | [卡片](devices/measurement-analyzers/thorlabs-pm100d-s154c.md) |
| R&S RTM3004 | `rs.rtm3004` | LAN / USB VISA | [卡片](devices/oscilloscopes/rtm3004.md) |
| R&S RTO64 | `rs.rto64` | LAN / USB VISA | [卡片](devices/oscilloscopes/rto64.md) |
| RIGOL DS1202Z（待确认后缀）/ DS1202Z-E | `rigol.ds1202ze` | DS1202Z-E：USB / LAN VISA | [卡片](devices/oscilloscopes/rigol-ds1202z.md) |
| Teledyne LeCroy WaveRunner 9254 | `lecroy.9254` | LAN VICP（1861）/ VISA VXI-11 或 USBTMC | [卡片](devices/oscilloscopes/waverunner-9254.md) |
| R&S ZNB3004 | `rs.znb3004` | LAN / USB VISA | [卡片](devices/measurement-analyzers/rs-znb3004.md) |
| Keysight / Agilent / HP 3458A | `keysight.3458a` | GPIB + USB-GPIB/LAN-GPIB 控制器 | [卡片](devices/measurement-analyzers/keysight-3458a.md) |
| Siglent SDG6032X | `siglent.sdg6032x` | USB / LAN VXI-11 / 已配置 TCP Socket | [卡片](devices/signal-sources/sdg6032x.md) |
| Keithley 2231A-30-3 | `keithley.2231a` | 2231A-001 USB 适配器 / 虚拟串口 | [卡片](devices/power-supplies/keithley-2231a.md) |
| Siglent SPD4306X | `siglent.spd4306x` | USB / LAN VISA / 已配置 TCP Socket | [卡片](devices/power-supplies/spd4306x.md) |
| ITECH IT6322 | `itech.it6322` | RS-232 / 已安装 USB 或 GPIB 接口 | [卡片](devices/power-supplies/it6322.md) |
| RIGOL DG5504 Pro | `rigol.dg5504pro` | LAN / USB；优先 PyVISA | [卡片](devices/signal-sources/dg5504-pro.md) |
| Tektronix AFG31252 | `tek.afg31252` | USB Device / LAN / GPIB（核对实际接口） | [卡片](devices/signal-sources/afg31252.md) |
| Siglent SDG2082X | `siglent.sdg2082x` | USB USBTMC / LAN VXI-11，经 PyVISA 接入 | [卡片](devices/signal-sources/sdg2082x.md) |
| R&S SMA100B | `rs.sma100b` | LAN / USB / 已安装 GPIB；优先 PyVISA | [卡片](devices/signal-sources/sma100b.md) |
| GW Instek GPD-4303S | `gwinstek.gpd4303s` | USB 虚拟串口；优先 PyVISA ASRL，9600 baud / 8N1 / LF | [卡片](devices/power-supplies/gpd-4303s.md) |
| RIGOL DSA815 | `rigol.dsa815` | LAN / USB；优先 PyVISA | [卡片](devices/spectrum-analyzers/rigol-dsa815.md) |
| Teledyne LeCroy WaveRunner 9404M | `lecroy.9404m` | LAN PyVISA/VXI-11（仪器选择 LXI） | [卡片](devices/oscilloscopes/waverunner-9404m.md) |
| RIGOL DHO924 | `rigol.dho924` | LAN PyVISA / VISA SOCKET | [卡片](devices/oscilloscopes/rigol-dho924.md) |
| R&S FPH02 | `rs.fph` | LAN；优先 PyVISA | [卡片](devices/spectrum-analyzers/rs-fph.md) |
| Ceyear 6362D | `ceyear.6362d` | LAN Socket 10001；PyVISA | [卡片](devices/measurement-analyzers/ceyear-6362d.md) |
| Microchip 53100A | `microchip.53100a` | USB：TimeLab 驱动与项目托管 TSERVE64；也支持已有外部 TSERVE | [卡片](devices/measurement-analyzers/phase-noise-53100a.md) |
| Joinwit JW8105 | `joinwit.jw8105` | RS232；RJ45 选件待核对 | [卡片](devices/measurement-analyzers/joinwit-jw8105.md) |
| 芯明天 XMT（完整型号待核） | `coremorrow.xmt` | USB 串口 / VISA ASRL；COM 参数由现场配置指定 | [卡片](devices/power-supplies/coremorrow-xmt.md) |
| RIGOL M300 | `rigol.m300` | LAN / USB VISA；优先 PyVISA | [卡片](devices/measurement-analyzers/rigol-m300.md) |
| K+K FXE | `kplusk.fxe` | Ethernet（经交换机直连设备 IP）；USB 仅用于官方配置工具 | [卡片](devices/measurement-analyzers/kplusk-fxe.md) |
| Keysight / Agilent 53220A | `keysight.53220a` | LAN VXI-11 / VISA SOCKET；优先 PyVISA | [卡片](devices/measurement-analyzers/keysight-53220a.md) |
<!-- device-modules:end -->

## 现场盘点与状态提升

记录完整型号、厂家、序列号、固件、接口/选件、Windows 设备管理器名称、驱动/DLL 位数及版本、VISA/COM/SDK 资源、当前输出和接线。照片、报告、采集文件保存在运行数据目录，不放进源码。

先按[实机指南](testing/hardware-testing.md)做只读检查，再逐项设置/采集/读回/故障恢复；报告关联到具体能力。全部基础验收完成后才能由现场记录确认“可控”，软件不会自动提升状态。
