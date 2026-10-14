# RIGOL M300

独立型号包，适配标识为 `rigol.m300`。协议和动态插槽盘点只在本包维护，公共层只负责 VISA 会话、身份匹配、占用锁和记录。

## 能力与协议

- [RIGOL M300 Programming Guide](https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/data-aquisition/M300_ProgrammingGuide_EN.pdf)，SCPI 命令系统及编程示例。
- 精确匹配厂家 `RIGOL TECHNOLOGIES`、型号 `M300`，通过 `*IDN?` 返回完整序列号和固件。
- 读取 100/200/300/400/500 插槽型号、DMM 安装/启用状态、扫描列表和大小、触发模式/间隔、监视状态及错误队列。
- 支持触发模式和扫描间隔的显式读写、读回核验；监视/DMM 状态写入需要确认并在实机计划中恢复。
- 被动标量采集使用当前设备扫描列表的 `READ?`。没有扫描列表时明确返回 `not_configured`，不会擅自写入通道或启动测量。

## 连接、范围和限制

优先使用 LAN/USB VISA。扫描只读取身份和模块状态，不执行 `*RST`、`SYST:PRESet`、`ROUT:SCAN`、`CONF`、`INIT` 或 `ABORT`。触发间隔范围按手册为 0–359999.999 s；软件测试可短暂设置后必须恢复原值。插槽型号、序列号和通道数量以现场读回为准，不从 IP 或目录名推断。

当前包不开放扫描列表编辑、模块复位、校准、存储器导出、数字输出和物理测量精度验收。未接外部负载/传感器或扫描列表为空时，采集及物理输出项目记录为 `blocked`/`not_tested`。

## 测试与部署

- 软件：`pytest -q src/lab_control/instruments/devices/rigol_m300 tests`。
- 实机：见 [hardware/README.md](hardware/README.md)；使用显式资源和完整序列号，按运行目录 `hardware-tests` 留档。
- 依赖：复用已登记的 `rs-visa`，不下载或复制厂商 DLL 到源码目录。
