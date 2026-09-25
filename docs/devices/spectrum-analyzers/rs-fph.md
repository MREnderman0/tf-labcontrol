<!-- Generated from scripts/generate_device_docs.py legacy metadata. -->

# R&S FPH

维护源：[旧型号文档元数据](../../../scripts/generate_device_docs.py)。本型号尚未迁入独立包，能力来自注册表；不要直接修改本页。

## 1. 型号与可信度

注册 ID：`rs.fph`。型号精确匹配；软件实现与具体资产的实测结果分别记录。附件简写和身份别名见本卡片说明。软件连接成功不等于验收通过。

## 2. 电脑物理连接

LAN / USB VISA（核对仪器当前接口配置）。接口和选件以设备背板、固件与现场盘点为准。

## 3. Windows 前置条件

64 位 VISA Runtime。安装步骤见[实机测试指南](../../testing/hardware-testing.md)；DLL 放在厂商安装位置，通过绝对路径加载，不复制进源码。

## 4. 资源与枚举

连接模板见[config/examples](../../../config/examples/README.md)。VISA 使用扫描返回的完整资源；串口填写确认的 COM/波特率；Pico 使用 `PICO::序列号`；PM100D 使用 SDK 枚举的资源；53100A 手工填写已部署服务地址。扫描只枚举 SDK，不打开 SDK 仪器。

## 5. 官方命令/API 依据

[厂家资料](https://scdn.rohde-schwarz.com/ur/pws/dl_downloads/pdm/cl_manuals/user_manual/1321_1011_01/FPH_User_Manual_en_21.pdf)；版本：1321.1011 revision 21。逐操作命令定位、参数和模板见[自动生成能力清单](../../generated/capability-matrix.json)。测试样本是按手册编写的夹具，尚非实机抓包。

## 6. 当前统一能力

逻辑类别 `spectrum_analyzer`；通道 `(1,)`。`None` 表示该侧未在通用参数层给出数值边界；适配器仍执行关联参数与通道校验。未列出的操作返回 `unsupported`。

| 操作 | 参数 / 单位 / 范围 | 作用域 | 方向 | 额外确认 | 状态 |
| --- | --- | --- | --- | --- | --- |
| `start_frequency` | value: number Hz [0, 2000000000.0] | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `stop_frequency` | value: number Hz [0, 2000000000.0] | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `center_frequency` | value: number Hz [0, 2000000000.0] | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `span` | value: number Hz [0, 2000000000.0] | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `rbw` | value: number Hz [100, 1000000.0] | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `vbw` | value: number Hz [10, 1000000.0] | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `rbw_auto` | value: boolean  [None, None] | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `vbw_auto` | value: boolean  [None, None] | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `reference_level` | value: number dBm [-100, 30] | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `attenuation` | value: number dB [0, 30] | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `attenuation_auto` | value: boolean  [None, None] | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `sweep_time` | value: number s [0.001, 1000] | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `continuous` | value: boolean  [None, None] | 整机 | 读/写 | 需要 | 软件已实现；实测按资产查证 |
| `marker_enabled` | value: boolean  [None, None] | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `marker_frequency` | value: number Hz [0, 2000000000.0] | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `single` | 无 | 整机 | /写 | 需要 | 软件已实现；实测按资产查证 |
| `marker_peak` | 无 | 整机 | /写 | 无需 | 软件已实现；实测按资产查证 |
| `marker_value` | 无 | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |

## 7. 控制、读回与采集

页面入口：**实时监测 / 频谱仪**。参数由当前注册能力生成；常用、高级、状态与诊断分组。只读采集能力通过监测页的单次/开始操作使用；缺选件项显示原因且禁用。详细操作见[控制与监测指南](../../guides/control-and-monitoring.md)。

采集路线：`fph`。`apply` 返回成功、拒绝、部分执行或不确定；只有提供读回命令的操作才有硬件读回，不能将 SDK 已接收设置描述成测量值。`status` 显式读取，扫描和总览定时刷新不会额外采集。

使用 FPH 手册，轨迹为 TRAC1:DATA?，不能套用 DSA815 的 TRACE1 参数。频率上限保守为 2 GHz，RBW/VBW 使用保守范围；扩展频率、前置放大和测量应用选件需现场核对后扩展。

## 8. 限制和故障处理

单设备单 worker 串行执行。数值范围是软件保守边界，仍需核对负载、频率、探头和接线条件。输出/触发等动作要求明确确认；超时不重发、不自动回滚，先诊断再重连。断开软件连接不表示输出关闭。错误数据使用质量标记，不能以零值代替。

## 9. Python 落点

通过 `ApplicationRuntime` → `SessionManager.submit()` → 注册表 `rs.fph` 调用；SCPI 型号共用所属类别的实现和独立 `Profile`，不创建空壳同名类。源码见 [instruments](../../../src/lab_control/instruments/)；示例见 [examples](../../../examples/README.md)。

## 10. Mock 与实机验收

软件：身份精确匹配、参数越界、未知能力和控制夹具；SDK 使用函数替身验证参数/句柄/缓冲区。完整结果以 `pytest` 报告为准，不能从该表推断全部高级命令已逐项验证。

现场按[统一验收步骤](../../testing/hardware-testing.md)记录完整身份、固件、驱动、资源、每个测试操作、请求、读回、原始错误、数据文件和恢复结果。按授权范围先只读，再控制/采集和恢复。`hardware_passed=false` 是保留的型号级兼容字段，不代表具体资产失败或尚未测试；逐项结论见运行目录摘要。
