<!-- Generated from an independent device package; edit its README.md. -->

维护源：[包内 README](../../../src/lab_control/instruments/devices/rigol_ds1202ze/README.md)。修改维护源后重新生成。

# RIGOL DS1202Z（待确认后缀）/ DS1202Z-E

注册 ID：`rigol.ds1202ze`。本包是协议、范围、样本和型号说明的维护源。

<a id="1-型号与可信度"></a>
<a id="5-官方命令api-依据"></a>
<a id="6-当前统一能力"></a>
## 能力与协议

逻辑类别 `oscilloscope`；通道 `[1, 2]`。命令与范围保持迁移前契约，见 [能力基线](../../../src/lab_control/instruments/devices/rigol_ds1202ze/tests/fixtures/capabilities-before.json) 和 [控制样本](../../../src/lab_control/instruments/devices/rigol_ds1202ze/tests/fixtures/control.json)。

[厂家资料](https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/oscilloscopes/DS1000ZE_ProgrammingGuide_EN.pdf)；版本：DS1000Z-E Programming Guide。样本是软件协议夹具，不是实机抓包。

<a id="2-电脑物理连接"></a>
<a id="3-windows-前置条件"></a>
<a id="4-资源与枚举"></a>
## 连接与驱动

DS1202Z-E：USB / LAN VISA；64 位 VISA Runtime。复用既有组件 `rs-visa`，材料、版本、哈希及验证状态以 [公共清单](../../../deployment/components.json) 为准；本次迁移不新增或升级依赖。现场接口、驱动安装与恢复仍须逐项核对。

<a id="7-控制读回与采集"></a>
## 单位与限制

附件原型号 DS1202Z 保留待补信息；仅 DS1202Z-E 身份可匹配此实现。两通道；当前读取 NORM 屏幕波形，不宣称导出全部存储深度。BYTE 缩放包含 YORigin/YREFerence。

数值范围和通道限制见能力基线；实验限制可进一步收紧。超时不重放设置，断开软件连接不等于输出关闭。

<a id="9-python-落点"></a>
## 文件与公共依赖

`adapter.py` / `protocol.py`（若有）维护本型号实现；`tests` 保存迁移契约和协议夹具；`hardware` 保存硬件计划及离线记录转换。公共层仅提供会话、传输、通用校验和存储，不依赖其他型号。

<a id="10-mock-与实机验收"></a>
## 测试入口与实机状态

软件回归：`.venv-dev/Scripts/python.exe -m pytest src/lab_control/instruments/devices/rigol_ds1202ze/tests tests/unit/test_adapters.py tests/unit/test_extended_protocols.py tests/unit/test_sdk.py`。

| 功能 | 接口及条件 | 验证范围 / 状态 | 证据位置 |
| --- | --- | --- | --- |
| 能力、身份、范围与协议 | Mock / SDK 替身 | 迁移前契约与离线回归；不代表实机验收 | 包内 tests 与公共集成测试 |
| 枚举、连接、读写、恢复、重连 | 每台实际资产分别核验 | 本次迁移未访问硬件 | 运行目录 hardware-tests |
| 物理输出或测量精度 | 接线及参考源待明确 | not_tested；软件通过不推断物理性能 | 运行目录批次 |

<a id="8-限制和故障处理"></a>
## 已知限制与待测

| 功能或问题 | 限制 / 原因 | 后续验证 |
| --- | --- | --- |
| 厂商固件及接口差异 | 沿用原实现，未新增实机证据 | 按 [硬件计划](../../../src/lab_control/instruments/devices/rigol_ds1202ze/hardware/README.md) 逐接口测试 |
| 采集与高级功能 | 附件原型号 DS1202Z 保留待补信息；仅 DS1202Z-E 身份可匹配此实现。两通道；当前读取 NORM 屏幕波形，不宣称导出全部存储深度。BYTE 缩放包含 YORigin/YREFerence。 | 不放宽既有限制；有依据后另行实现 |

## 扩展与移除

修改本包并更新唯一 catalog 注册与生成文档。移除时保留用户配置及实机历史；旧导入位置仅为兼容转导出。

## 注册能力（自动生成）

范围、单位及命令来自当前注册定义；资产级实测结果见运行目录。

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
