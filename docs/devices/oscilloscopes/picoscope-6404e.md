<!-- Generated from an independent device package; edit its README.md. -->

维护源：[包内 README](../../../src/lab_control/instruments/devices/pico_6404e/README.md)。修改维护源后重新生成。

# PicoScope 6404E

注册 ID：`pico.6404e`。本包是协议、范围、样本和型号说明的维护源。

<a id="1-型号与可信度"></a>
<a id="5-官方命令api-依据"></a>
<a id="6-当前统一能力"></a>
## 能力与协议

逻辑类别 `oscilloscope`；通道 `[1, 2, 3, 4]`。命令与范围保持迁移前契约，见 [能力基线](../../../src/lab_control/instruments/devices/pico_6404e/tests/fixtures/capabilities-before.json) 和 [控制样本](../../../src/lab_control/instruments/devices/pico_6404e/tests/fixtures/control.json)。

[厂家资料](https://www.picotech.com/helpfiles/6000a-api/)；版本：SDK headers/examples (existing implementation)。样本是软件协议夹具，不是实机抓包。

<a id="2-电脑物理连接"></a>
<a id="3-windows-前置条件"></a>
<a id="4-资源与枚举"></a>
## 连接与驱动

`pico` 传输在扫描阶段仅保留枚举候选；型号元数据要求用户显式连接后核验身份，不由扫描创建 SDK 仪器会话。

USB 3.x + ps6000a SDK；PicoSDK 64 位、ps6000a.dll 和 USB 驱动。复用既有组件 `pico-sdk`，材料、版本、哈希及验证状态以 [公共清单](../../../deployment/components.json) 为准；本次迁移不新增或升级依赖。现场接口、驱动安装与恢复仍须逐项核对。

<a id="7-控制读回与采集"></a>
## 单位与限制

显式选择序列号，使用 8 位模式；先设置通道，再 block 或 streaming。6404E 的 1 MΩ 量程为 ±10 mV～±20 V，50 Ω 最大 ±5 V；偏移上限在 ≤1 V 量程为 ±1.25 V，较大量程为 ±20 V，依据[6000E 数据表第 31 页](https://www.picotech.com/download/datasheets/picoscope-6000e-series-data-sheet.pdf)。不能使用共享 SDK 枚举中的 50 V 量程。触发阈值按配置偏移转换为 ADC 计数。SDK 缓冲区保持到 Stop 和解除注册后才释放。streaming 为有限批次，批次间有间隔，尚未承诺无缝长时间数据流。

数值范围和通道限制见能力基线；实验限制可进一步收紧。超时不重放设置，断开软件连接不等于输出关闭。

<a id="9-python-落点"></a>
## 文件与公共依赖

`adapter.py` / `protocol.py`（若有）维护本型号实现；`tests` 保存迁移契约和协议夹具；`hardware` 保存硬件计划及离线记录转换。公共层仅提供会话、传输、通用校验和存储，不依赖其他型号。

<a id="10-mock-与实机验收"></a>
## 测试入口与实机状态

软件回归：`.venv-dev/Scripts/python.exe -m pytest src/lab_control/instruments/devices/pico_6404e/tests tests/unit/test_adapters.py tests/unit/test_extended_protocols.py tests/unit/test_sdk.py`。

| 功能 | 接口及条件 | 验证范围 / 状态 | 证据位置 |
| --- | --- | --- | --- |
| 能力、身份、范围与协议 | Mock / SDK 替身 | 迁移前契约与离线回归；不代表实机验收 | 包内 tests 与公共集成测试 |
| 枚举、连接、读写、恢复、重连 | 每台实际资产分别核验 | 本次迁移未访问硬件 | 运行目录 hardware-tests |
| 物理输出或测量精度 | 接线及参考源待明确 | not_tested；软件通过不推断物理性能 | 运行目录批次 |

<a id="8-限制和故障处理"></a>
## 已知限制与待测

| 功能或问题 | 限制 / 原因 | 后续验证 |
| --- | --- | --- |
| 厂商固件及接口差异 | 沿用原实现，未新增实机证据 | 按 [硬件计划](../../../src/lab_control/instruments/devices/pico_6404e/hardware/README.md) 逐接口测试 |
| 采集与高级功能 | 显式选择序列号，使用 8 位模式；先设置通道，再 block 或 streaming。6404E 的 1 MΩ 量程为 ±10 mV～±20 V，50 Ω 最大 ±5 V；偏移上限在 ≤1 V 量程为 ±1.25 V，较大量程为 ±20 V，依据[6000E 数据表第 31 页](https://www.picotech.com/download/datasheets/picoscope-6000e-series-data-sheet.pdf)。不能使用共享 SDK 枚举中的 50 V 量程。触发阈值按配置偏移转换为 ADC 计数。SDK 缓冲区保持到 Stop 和解除注册后才释放。streaming 为有限批次，批次间有间隔，尚未承诺无缝长时间数据流。 | 不放宽既有限制；有依据后另行实现 |

## 扩展与移除

修改本包并更新唯一 catalog 注册与生成文档。移除时保留用户配置及实机历史；旧导入位置仅为兼容转导出。

## 注册能力（自动生成）

范围、单位及命令来自当前注册定义；资产级实测结果见运行目录。

| 操作 | 参数 / 单位 / 范围 | 作用域 | 方向 | 额外确认 | 状态 |
| --- | --- | --- | --- | --- | --- |
| `channel` | enabled: boolean  [None, None]; range: enum  0.01/0.02/0.05/0.1/0.2/0.5/1/2/5/10/20; coupling: enum  AC/DC/DC50; offset: number V [-20, 20] | 通道 | /写 | 无需 | 软件已实现；实测按资产查证 |
| `timebase` | value: integer  [0, 1000000] | 整机 | /写 | 无需 | 软件已实现；实测按资产查证 |
| `sample_interval` | value: number s [1e-09, 1] | 整机 | /写 | 无需 | 软件已实现；实测按资产查证 |
| `trigger` | enabled: boolean  [None, None]; threshold: number V [-20, 20]; direction: enum  RISING/FALLING; timeout_us: integer  [0, 10000000] | 通道 | /写 | 无需 | 软件已实现；实测按资产查证 |
| `block` | 无 | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `streaming` | 无 | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
