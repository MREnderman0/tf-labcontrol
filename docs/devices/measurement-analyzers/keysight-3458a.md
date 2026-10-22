<!-- Generated from an independent device package; edit its README.md. -->

维护源：[包内 README](../../../src/lab_control/instruments/devices/keysight_3458a/README.md)。修改维护源后重新生成。

# Keysight / Agilent / HP 3458A

注册 ID：`keysight.3458a`。本包是协议、范围、样本和型号说明的维护源。

<a id="1-型号与可信度"></a>
<a id="5-官方命令api-依据"></a>
<a id="6-当前统一能力"></a>
## 能力与协议

逻辑类别 `multimeter`；通道 `[1]`。命令与范围保持迁移前契约，见 [能力基线](../../../src/lab_control/instruments/devices/keysight_3458a/tests/fixtures/capabilities-before.json) 和 [控制样本](../../../src/lab_control/instruments/devices/keysight_3458a/tests/fixtures/control.json)。

[厂家资料](https://www.keysight.com/us/en/assets/9018-01343/user-manuals/9018-01343.pdf)；版本：03458-90014, Command reference。样本是软件协议夹具，不是实机抓包。

<a id="2-电脑物理连接"></a>
<a id="3-windows-前置条件"></a>
<a id="4-资源与枚举"></a>
## 连接与驱动

GPIB + USB-GPIB/LAN-GPIB 控制器；控制器驱动和 64 位 VISA Runtime。复用既有组件 `rs-visa`, `gpib-interface`，材料、版本、哈希及验证状态以 [公共清单](../../../deployment/components.json) 为准；本次迁移不新增或升级依赖。现场接口、驱动安装与恢复仍须逐项核对。

<a id="7-控制读回与采集"></a>
## 单位与限制

使用原生 ID?/FUNC/NPLC/NRDGS/RMEM，不发送 *IDN?。HP 3458A/HP3458A 响应归一为型号；无序列号时不自动确认物理同一性。FUNC? 同时返回功能码和量程。读取 RMEM 会停止内部存储，故即使不 initiate 也需 --confirm。

数值范围和通道限制见能力基线；实验限制可进一步收紧。超时不重放设置，断开软件连接不等于输出关闭。

<a id="9-python-落点"></a>
## 文件与公共依赖

`adapter.py` / `protocol.py`（若有）维护本型号实现；`tests` 保存迁移契约和协议夹具；`hardware` 保存硬件计划及离线记录转换。公共层仅提供会话、传输、通用校验和存储，不依赖其他型号。

<a id="10-mock-与实机验收"></a>
## 测试入口与实机状态

软件回归：`.venv-dev/Scripts/python.exe -m pytest src/lab_control/instruments/devices/keysight_3458a/tests tests/unit/test_adapters.py tests/unit/test_extended_protocols.py tests/unit/test_sdk.py`。

| 功能 | 接口及条件 | 验证范围 / 状态 | 证据位置 |
| --- | --- | --- | --- |
| 能力、身份、范围与协议 | Mock / SDK 替身 | 迁移前契约与离线回归；不代表实机验收 | 包内 tests 与公共集成测试 |
| 枚举、连接、读写、恢复、重连 | 每台实际资产分别核验 | 本次迁移未访问硬件 | 运行目录 hardware-tests |
| 物理输出或测量精度 | 接线及参考源待明确 | not_tested；软件通过不推断物理性能 | 运行目录批次 |

<a id="8-限制和故障处理"></a>
## 已知限制与待测

| 功能或问题 | 限制 / 原因 | 后续验证 |
| --- | --- | --- |
| 厂商固件及接口差异 | 沿用原实现，未新增实机证据 | 按 [硬件计划](../../../src/lab_control/instruments/devices/keysight_3458a/hardware/README.md) 逐接口测试 |
| 采集与高级功能 | 使用原生 ID?/FUNC/NPLC/NRDGS/RMEM，不发送 *IDN?。HP 3458A/HP3458A 响应归一为型号；无序列号时不自动确认物理同一性。FUNC? 同时返回功能码和量程。读取 RMEM 会停止内部存储，故即使不 initiate 也需 --confirm。 | 不放宽既有限制；有依据后另行实现 |

## 扩展与移除

修改本包并更新唯一 catalog 注册与生成文档。移除时保留用户配置及实机历史；旧导入位置仅为兼容转导出。

## 注册能力（自动生成）

范围、单位及命令来自当前注册定义；资产级实测结果见运行目录。

| 操作 | 参数 / 单位 / 范围 | 作用域 | 方向 | 额外确认 | 状态 |
| --- | --- | --- | --- | --- | --- |
| `function` | value: enum  DCV/ACV/ACDCV/DCI/ACI/ACDCI/OHM/OHMF/FREQ/PER | 整机 | /写 | 无需 | 软件已实现；实测按资产查证 |
| `range` | value: number function-dependent [1e-09, 1000000000.0] | 整机 | /写 | 无需 | 软件已实现；实测按资产查证 |
| `autorange` | value: enum  ON/OFF | 整机 | /写 | 无需 | 软件已实现；实测按资产查证 |
| `integration` | value: number PLC [0.0001, 1000] | 整机 | /写 | 无需 | 软件已实现；实测按资产查证 |
| `resolution` | value: number % [1e-06, 100] | 整机 | /写 | 无需 | 软件已实现；实测按资产查证 |
| `autozero` | value: enum  ON/OFF/ONCE | 整机 | /写 | 无需 | 软件已实现；实测按资产查证 |
| `delay` | value: number s [0, 6000] | 整机 | /写 | 无需 | 软件已实现；实测按资产查证 |
| `trigger_source` | value: enum  AUTO/EXT/HOLD/SYN | 整机 | /写 | 需要 | 软件已实现；实测按资产查证 |
| `trigger_arm` | value: enum  AUTO/EXT/HOLD | 整机 | /写 | 需要 | 软件已实现；实测按资产查证 |
| `memory` | value: enum  OFF/FIFO/LIFO | 整机 | /写 | 无需 | 软件已实现；实测按资产查证 |
| `output_format` | value: enum  ASCII | 整机 | /写 | 无需 | 软件已实现；实测按资产查证 |
| `function_status` | 无 | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `integration_status` | 无 | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `errors` | 无 | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `temperature` | 无 | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `sample_count` | value: integer  [1, 1000000] | 整机 | /写 | 无需 | 软件已实现；实测按资产查证 |
