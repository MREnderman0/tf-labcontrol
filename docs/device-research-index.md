# 设备接入索引（0.3.0）

这是附件设备条目的唯一索引；具体能力由[实际矩阵](generated/capability-matrix.md)生成，型号卡片记录连接和验收。附件空白指令/载荷数据表、历史日期和疑似资产号不被当成已确认协议。

“已实现”表示存在真实传输与型号代码；“软件测试”不等于“实机通过”。具体资产及接口的验证结果以运行目录中的批次记录为准。

0.3.0 分类控制与实时监测已接入上述能力；每张型号卡片说明页面入口，能力 JSON 逐项记录中文标签和分组。使用方法见[控制与监测指南](control-and-monitoring.md)。未知型号仍只有隔离模拟示例。

| 类别 | 附件设备 | 电脑交互方式 | 实际注册 ID / 状态 | 归档 |
| --- | --- | --- | --- | --- |
| 信号源 | SDG6032X | USB/LAN VXI-11/Socket | siglent.sdg6032x；待实机 | [卡片](devices/signal-sources/sdg6032x.md) |
| 信号源 | DG5504 Pro | USB/LAN VISA/Socket | rigol.dg5504pro；待实机 | [卡片](devices/signal-sources/dg5504-pro.md) |
| 电源 | GPD-4303S | USB 虚拟 COM / ASRL，厂家文本协议 | gwinstek.gpd4303s；待实机 | [卡片](devices/power-supplies/gpd-4303s.md) |
| 电源 | Keithley 2231A | 2231A-001 USB 适配器/COM | keithley.2231a；仅精确匹配 2231A-30-3 | [卡片](devices/power-supplies/keithley-2231a.md) |
| 电源 | PSW160 | 待确认厂家、完整型号与接口 | 不推定为 EA 或其他厂家；不注册真实协议 | [待补卡片](devices/power-supplies/psw160.md) |
| 电源 | SPD4306X | USB/LAN VISA/Socket | siglent.spd4306x；待实机 | [卡片](devices/power-supplies/spd4306x.md) |
| 电源 | IT6322 | RS-232/实际已安装 USB/GPIB | itech.it6322；独立旧型号协议 | [卡片](devices/power-supplies/it6322.md) |
| 示波器 | PicoScope 6404E | USB + ps6000a SDK | pico.6404e；待实机 | [卡片](devices/oscilloscopes/picoscope-6404e.md) |
| 示波器 | RTM3004 | LAN/USB VISA/SCPI | rs.rtm3004；待实机 | [卡片](devices/oscilloscopes/rtm3004.md) |
| 示波器 | RTO64 | LAN/USB VISA/SCPI | rs.rto64；RTO6 手册，待实机 | [卡片](devices/oscilloscopes/rto64.md) |
| 示波器 | WaveRunner 9254 | LAN VICP/VXI-11/USBTMC | lecroy.9254；待实机 | [卡片](devices/oscilloscopes/waverunner-9254.md) |
| 示波器 | WaveRunner 9404M | LAN VICP/VXI-11/USBTMC | lecroy.9404m；待实机 | [卡片](devices/oscilloscopes/waverunner-9404m.md) |
| 示波器 | DS1202Z | 原条目待确认后缀；DS1202Z-E 为 USB/LAN VISA | 仅注册 rigol.ds1202ze；不静默改写原型号 | [共同归档](devices/oscilloscopes/rigol-ds1202z.md) |
| 频谱仪 | DSA815 | USB/LAN/已安装 GPIB + VISA | rigol.dsa815；待实机 | [卡片](devices/spectrum-analyzers/rigol-dsa815.md) |
| 频谱仪 | R&S FPH | LAN/USB VISA（核对当前接口） | rs.fph；待实机 | [卡片](devices/spectrum-analyzers/rs-fph.md) |
| 光功率 | PM100D + S154C | 控制台 USB + TLPMX/旧 PM100D SDK；探头接控制台 | thorlabs.pm100d；待实机 | [卡片](devices/measurement-analyzers/thorlabs-pm100d-s154c.md) |
| 光功率 | 多通道光功率计 | 待完整型号与 SDK/协议 | 仅类别模拟 | [待补卡片](devices/measurement-analyzers/multi-channel-optical-power-meter.md) |
| 万用表 | Keysight 3458A | GPIB 控制器/VISA，原生非 SCPI 命令 | keysight.3458a；待实机 | [卡片](devices/measurement-analyzers/keysight-3458a.md) |
| 网络分析 | ZNB3004 | LAN/USB VISA/SCPI | rs.znb3004；ZNB3000 手册，待实机 | [卡片](devices/measurement-analyzers/rs-znb3004.md) |
| 待确认类别 | GOCA-8-NK2603212 | 待厂家和型号/资产号划分 | 不注册真实命令 | [待补卡片](devices/measurement-analyzers/goca-8.md) |
| 相噪候选 | 53100A | Microchip 候选：已部署 TSERVE 命令/数据端口 | microchip.53100a；现场确认身份后启用 | [卡片](devices/measurement-analyzers/phase-noise-53100a.md) |
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
| Tektronix AFG31252 | `tek.afg31252` | USB Device / LAN / GPIB（核对实际接口） | [卡片](devices/signal-sources/afg31252.md) |
| Siglent SDG2082X | `siglent.sdg2082x` | USB USBTMC / LAN VXI-11，经 PyVISA 接入 | [卡片](devices/signal-sources/sdg2082x.md) |
| R&S SMA100B | `rs.sma100b` | LAN / USB / 已安装 GPIB；优先 PyVISA | [卡片](devices/signal-sources/sma100b.md) |
<!-- device-modules:end -->

## 现场盘点与状态提升

记录完整型号、厂家、序列号、固件、接口/选件、Windows 设备管理器名称、驱动/DLL 位数及版本、VISA/COM/SDK 资源、当前输出和接线。照片、报告、采集文件保存在运行数据目录，不放进源码。

先按[实机指南](hardware-testing.md)做只读检查，再逐项设置/采集/读回/故障恢复；报告关联到具体能力。全部基础验收完成后才能由现场记录确认“可控”，软件不会自动提升状态。
