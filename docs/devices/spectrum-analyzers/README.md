# 频谱仪

频谱仪统一实现 `SpectrumAnalyzer` 能力：起止频率、中心/跨度、RBW、扫描、轨迹、频率轴和配置快照。

| 型号 | 接入路线 | 状态 | 文档 |
| --- | --- | --- | --- |
| RIGOL DSA815 | USB/LAN/GPIB/VISA/SCPI | 资料确认，待实机验证 | [DSA815](rigol-dsa815.md) |
| R&S FPH | LAN/USB/VISA/SCPI | 资料确认，待实机验证 | [FPH](rs-fph.md) |

频谱轨迹必须保存频率轴、点数、RBW/VBW（若支持）、参考电平、扫描时间和单位，不能只保存幅度数组。
