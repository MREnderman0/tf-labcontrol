# R&S RTM3004

注册 ID：`rs.rtm3004`。本包是协议、范围、样本和型号说明的维护源。

<a id="1-型号与可信度"></a>
<a id="5-官方命令api-依据"></a>
<a id="6-当前统一能力"></a>
## 能力与协议

逻辑类别 `oscilloscope`；通道 `[1, 2, 3, 4]`。命令与范围保持迁移前契约，见 [能力基线](tests/fixtures/capabilities-before.json) 和 [控制样本](tests/fixtures/control.json)。

[厂家资料](https://scdn.rohde-schwarz.com/ur/pws/dl_downloads/pdm/cl_manuals/user_manual/1335_9090_01/RTM3000_UserManual_en_12.pdf)；版本：1335.9090 revision 12。样本是软件协议夹具，不是实机抓包。

<a id="2-电脑物理连接"></a>
<a id="3-windows-前置条件"></a>
<a id="4-资源与枚举"></a>
## 连接与驱动

LAN / USB VISA；64 位 VISA Runtime。复用既有组件 `rs-visa`，材料、版本、哈希及验证状态以 [公共清单](../../../../../deployment/components.json) 为准；本次迁移不新增或升级依赖。现场接口、驱动安装与恢复仍须逐项核对。

<a id="7-控制读回与采集"></a>
## 单位与限制

探头倍率 PROB:SET:ATT:MAN；导出 ASCII 电压及 XINC/XOR 时间轴。支持平均、采样/峰值/高分辨率设置；当前导出只接受每点一个值，峰值/包络双值数据会拒绝。分段开关需要 segmented_memory 选件，逐段时间戳导出待扩展。

数值范围和通道限制见能力基线；实验限制可进一步收紧。超时不重放设置，断开软件连接不等于输出关闭。

<a id="9-python-落点"></a>
## 文件与公共依赖

`adapter.py` / `protocol.py`（若有）维护本型号实现；`tests` 保存迁移契约和协议夹具；`hardware` 保存硬件计划及离线记录转换。公共层仅提供会话、传输、通用校验和存储，不依赖其他型号。

<a id="10-mock-与实机验收"></a>
## 测试入口与实机状态

软件回归：`.venv-dev/Scripts/python.exe -m pytest src/lab_control/instruments/devices/rs_rtm3004/tests tests/unit/test_adapters.py tests/unit/test_extended_protocols.py tests/unit/test_sdk.py`。

| 功能 | 接口及条件 | 验证范围 / 状态 | 证据位置 |
| --- | --- | --- | --- |
| 能力、身份、范围与协议 | Mock / SDK 替身 | 迁移前契约与离线回归；不代表实机验收 | 包内 tests 与公共集成测试 |
| 枚举、连接、读写、恢复、重连 | 每台实际资产分别核验 | 本次迁移未访问硬件 | 运行目录 hardware-tests |
| 物理输出或测量精度 | 接线及参考源待明确 | not_tested；软件通过不推断物理性能 | 运行目录批次 |

<a id="8-限制和故障处理"></a>
## 已知限制与待测

| 功能或问题 | 限制 / 原因 | 后续验证 |
| --- | --- | --- |
| 厂商固件及接口差异 | 沿用原实现，未新增实机证据 | 按 [硬件计划](hardware/README.md) 逐接口测试 |
| 采集与高级功能 | 探头倍率 PROB:SET:ATT:MAN；导出 ASCII 电压及 XINC/XOR 时间轴。支持平均、采样/峰值/高分辨率设置；当前导出只接受每点一个值，峰值/包络双值数据会拒绝。分段开关需要 segmented_memory 选件，逐段时间戳导出待扩展。 | 不放宽既有限制；有依据后另行实现 |

## 扩展与移除

修改本包并更新唯一 catalog 注册与生成文档。移除时保留用户配置及实机历史；旧导入位置仅为兼容转导出。
