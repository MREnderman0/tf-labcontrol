<!-- Generated from scripts/generate_device_docs.py legacy metadata. -->

# RIGOL DS1202Z（待确认后缀）/ DS1202Z-E

维护源：[旧型号文档元数据](../../../scripts/generate_device_docs.py)。本型号尚未迁入独立包，能力来自注册表；不要直接修改本页。

## 1. 型号与可信度

注册 ID：`rigol.ds1202ze`。型号精确匹配；软件实现与具体资产的实测结果分别记录。附件简写和身份别名见本卡片说明。软件连接成功不等于验收通过。

## 2. 电脑物理连接

DS1202Z-E：USB / LAN VISA。接口和选件以设备背板、固件与现场盘点为准。

## 3. Windows 前置条件

64 位 VISA Runtime。安装步骤见[实机测试指南](../../testing/hardware-testing.md)；DLL 放在厂商安装位置，通过绝对路径加载，不复制进源码。

## 4. 资源与枚举

连接模板见[config/examples](../../../config/examples/README.md)。VISA 使用扫描返回的完整资源；串口填写确认的 COM/波特率；Pico 使用 `PICO::序列号`；PM100D 使用 SDK 枚举的资源。扫描只枚举 SDK，不打开 SDK 仪器。

## 5. 官方命令/API 依据

[厂家资料](https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/oscilloscopes/DS1000ZE_ProgrammingGuide_EN.pdf)；版本：DS1000Z-E Programming Guide。逐操作命令定位、参数和模板见[自动生成能力清单](../../generated/capability-matrix.json)。测试样本是按手册编写的夹具，尚非实机抓包。

## 6. 当前统一能力

逻辑类别 `oscilloscope`；通道 `(1, 2)`。`None` 表示该侧未在通用参数层给出数值边界；适配器仍执行关联参数与通道校验。未列出的操作返回 `unsupported`。

| 操作 | 参数 / 单位 / 范围 | 作用域 | 方向 | 额外确认 | 状态 |
| --- | --- | --- | --- | --- | --- |
| `channel_enabled` | value: boolean  [None, None] | 通道 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `scale` | value: number V/div [0.001, 10] | 通道 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `offset` | value: number V [-100, 100] | 通道 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `coupling` | value: enum  AC/DC/GND | 通道 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `probe_attenuation` | value: enum  0.01/0.1/1/10/100/1000 | 通道 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `bandwidth_limit` | value: enum  OFF/20M | 通道 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `invert` | value: boolean  [None, None] | 通道 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `timebase` | value: number s/div [5e-09, 50] | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `time_position` | value: number s [-100, 100] | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `trigger_source` | value: enum  CHAN1/CHAN2/EXT/AC | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `trigger_slope` | value: enum  POS/NEG/RFAL | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `trigger_level` | value: number V [-100, 100] | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `trigger_mode` | value: enum  AUTO/NORM/SING | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `acquisition_mode` | value: enum  NORM/AVER/PEAK/HRES | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `averages` | value: enum  2/4/8/16/32/64/128/256/512/1024 | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `record_length` | value: enum  AUTO/12000/120000/1200000/12000000/24000000 | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `run` | 无 | 整机 | /写 | 需要 | 软件已实现；实测按资产查证 |
| `stop` | 无 | 整机 | /写 | 需要 | 软件已实现；实测按资产查证 |
| `single` | 无 | 整机 | /写 | 需要 | 软件已实现；实测按资产查证 |
| `force_trigger` | 无 | 整机 | /写 | 需要 | 软件已实现；实测按资产查证 |
| `trigger_status` | 无 | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |

## 7. 控制、读回与采集

页面入口：**实时监测 / 示波器**。参数由当前注册能力生成；常用、高级、状态与诊断分组。只读采集能力通过监测页的单次/开始操作使用；缺选件项显示原因且禁用。详细操作见[控制与监测指南](../../guides/control-and-monitoring.md)。

采集路线：`rigol_scope`。`apply` 返回成功、拒绝、部分执行或不确定；只有提供读回命令的操作才有硬件读回，不能将 SDK 已接收设置描述成测量值。`status` 显式读取，扫描和总览定时刷新不会额外采集。

附件原型号 DS1202Z 保留待补信息；仅 DS1202Z-E 身份可匹配此实现。两通道；当前读取 NORM 屏幕波形，不宣称导出全部存储深度。BYTE 缩放包含 YORigin/YREFerence。

## 8. 限制和故障处理

单设备单 worker 串行执行。数值范围是软件保守边界，仍需核对负载、频率、探头和接线条件。输出/触发等动作要求明确确认；超时不重发、不自动回滚，先诊断再重连。断开软件连接不表示输出关闭。错误数据使用质量标记，不能以零值代替。

## 9. Python 落点

通过 `ApplicationRuntime` → `SessionManager.submit()` → 注册表 `rigol.ds1202ze` 调用；SCPI 型号共用所属类别的实现和独立 `Profile`，不创建空壳同名类。源码见 [instruments](../../../src/lab_control/instruments/)；示例见 [examples](../../../examples/README.md)。

## 10. Mock 与实机验收

软件：身份精确匹配、参数越界、未知能力和控制夹具；SDK 使用函数替身验证参数/句柄/缓冲区。完整结果以 `pytest` 报告为准，不能从该表推断全部高级命令已逐项验证。

现场按[统一验收步骤](../../testing/hardware-testing.md)记录完整身份、固件、驱动、资源、每个测试操作、请求、读回、原始错误、数据文件和恢复结果。按授权范围先只读，再控制/采集和恢复。`hardware_passed=false` 是保留的型号级兼容字段，不代表具体资产失败或尚未测试；逐项结论见运行目录摘要。
