# 示波器

示波器统一实现 `Oscilloscope` 能力：通道、量程、时基、触发、单次采集、波形读取、时间轴和电压缩放。波形数据必须保存前导信息和配置快照。

| 型号 | 接入路线 | 状态 | 文档 |
| --- | --- | --- | --- |
| PicoScope 6404E | USB + PicoSDK `ps6000a` | SDK 路线，待实机验证 | [6404E](picoscope-6404e.md) |
| R&S RTM3004 | LAN/USB/VISA/SCPI | 资料确认，待实机验证 | [RTM3004](rtm3004.md) |
| R&S RTO64 | LAN/USB/VISA/SCPI | 资料确认，待实机验证 | [RTO64](rto64.md) |
| WaveRunner 9254 | VICP/VXI-11/USBTMC | 待实机验证 | [9254](waverunner-9254.md) |
| WaveRunner 9404M | VICP/VXI-11/USBTMC/GPIB | 资料确认，待实机验证 | [9404M](waverunner-9404m.md) |
| RIGOL DS1202Z | USB/LAN/VISA/SCPI | 待实机验证 | [DS1202Z](rigol-ds1202z.md) |

PicoScope 使用厂商 SDK 事件/回调，其他型号优先使用 VISA 会话。所有适配器都通过同一个波形领域模型输出数据。
