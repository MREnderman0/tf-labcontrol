# SIGLENT SDG2082X

注册标识：`siglent.sdg2082x`。本目录集中保存双通道基础/高级兼容层、文档、测试和硬件验收入口，不复用 SDG6032X 的型号能力或范围。

## 能力与协议

`adapter.py`、`advanced.py` 和 `protocol.py` 提供基础波形、电参数、AM/FM/PM、扫频、突发及任意波形上传/选择。负载与通道耦合保持只读。

- 正弦最高 80 MHz、方波/脉冲 25 MHz、斜坡 1 MHz；DC 不提供频率和幅度设置。
- 幅度统一为 Vpp。按 HiZ/50 Ω 负载及 20 MHz 分界校验幅度上限，并检查幅度与偏置的联合限制；其他未核实负载拒绝参数写入。
- 通道跟踪或耦合打开时拒绝独立参数修改；输出 OFF 仍可执行。
- 复合响应解析兼容有/无通道头；状态读取考虑 DC 和噪声响应中缺少的周期参数，不伪造零值。
- 相位使用度；脉冲占空比/脉宽关联周期和边沿校验，斜坡对称度仅用于 RAMP。
- 整机参考源 `reference_clock` 使用 `ROSC?` 及 `ROSC INT/EXT`（编程手册 §3.20）。兼容有/无响应头，解析附带的 `10MOUT` 状态但不写入参考输出开关；参考源读回不代表外部参考已锁定。当前仅完成协议及界面 Mock 验证，未切换真实仪器参考源。
- 基础波形及其参数修改要求先关闭高级模式；频率变化也检查已有脉宽。扫频端点和 FM 频偏涉及的最高频率同时检查载波幅度与负载包络。
- 按数据表的 1 μHz 频率分辨率和 0.001% 脉冲占空比分辨率处理半步量化；保留请求值和实际读回。超出已知量化范围仍判为不一致，不自动重发。
- 调制、扫频、突发须显式启用且互斥；关闭状态缺少的高级参数返回 unavailable，不补零，不通过读参数隐式开启模式。调制参数与 CARR 载波字段分别解析。
- AM/FM/PM 使用独立类型、源、内部波形、频率与深度/偏移；FM 频偏检查载波到零点及该波形带宽的余量。
- 扫频支持 LINE/LOG、UP/DOWN、端点、时间和触发源；突发支持 NCYC/GATE、周期数、延时、起始相位和触发源。固件 2.01.01.35R3B2 在 MAN/EXT 下不返回 PRD，突发周期写入要求 INT。
- 任意波形上传实现使用小端 signed16，接受 8…131072 个归一化样点，拒绝覆盖同名文件，完整读回逐字节核验；上传和选择要求输出关闭且高级模式关闭。上传不附加换行、不自动开启输出。此软件上限不是仪器总存储深度。

依据：[编程手册 PG02-E05A](https://siglentna.com/wp-content/uploads/dlm_uploads/2023/01/SDG_Programming-Guide_PG02-E05A-12.pdf)、[SDG2000X EN02H 数据表](https://siglentna.com/wp-content/uploads/dlm_uploads/2025/11/SDG2000X_DataSheet_EN02H.pdf)。

## 连接与驱动

USB USBTMC 或 LAN VXI-11，优先 PyVISA；复用可用的 x64 VISA 和 USBTMC 驱动。使用单设备配置指定完整型号、实际资源和预期序列号，不根据系列名宽泛匹配。

## 文件与公共依赖

`__init__.py` 声明注册/文档元数据；型号包保存范围、解析和写入约束；`tests/` 保存专属测试，`tests/fixtures/` 保留迁移前基线及扩展后的 `capabilities-current.json`；`hardware/` 提供只读复核与输出关闭的验收工具。

公共依赖仅包括领域类型、SCPI 基类和 `common/siglent.py` 的复合字段解析；该解析器不含型号名或型号范围。模块不依赖其他型号包或旧信号源集合。

电子学页面仅创建波形、频率、幅度、偏移和相位编辑器；参考源放在整机“高级设置”弹窗中，显式应用后读回。关闭弹窗丢弃未应用选择，保留主卡片草稿。其他模式仍可使用已有高级功能；页面精简不删除协议能力，也不自动关闭调制、扫频或突发。

## 测试入口与实机状态

所有资产按[统一记录规范](../../../../../docs/testing/records.md)维护当前结论、接口矩阵、功能覆盖、已知问题及批次索引；补测更新对应表格，历史批次不改写。

```powershell
.venv-dev\Scripts\python.exe -m pytest src/lab_control/instruments/devices/siglent_sdg2082x/tests -q
.venv-dev\Scripts\python.exe -m lab_control.instruments.devices.siglent_sdg2082x.hardware.readonly --config '<运行目录内单设备配置>'
.venv-dev\Scripts\python.exe -m lab_control.instruments.devices.siglent_sdg2082x.hardware.validate_control --config '<运行目录内单设备配置>' --preflight-only
.venv-dev\Scripts\python.exe -m lab_control.instruments.devices.siglent_sdg2082x.hardware.validate_control --config '<运行目录内单设备配置>'
```

| 项目 | 验证范围、条件及限制 |
| --- | --- |
| LAN 补测 | 2026-09-25：基础、AM/FM/PM、扫频、已支持突发及 30 轮控制通过；恢复无差异、60 次读取及 3 次重连通过，两路输出 OFF。先前入口配置类型不一致在开通信前受阻，零设置写入，另批保留。 |
| 历史验证范围 | 2026-09-24 已测资产在输出关闭条件下通过双通道相位、占空比、脉宽、斜坡对称度、非零偏置、AM/FM/PM、扫频及计数/门控突发控制；30 轮交替读写、逐项恢复、60 次连续读取和 3 次重连通过。突发周期在本轮 MAN/EXT 触发范围内未测。完整身份、初始/最终状态、脚本及通信证据保存在运行目录各批次中；不将真实身份复制进源码。 |
| 记录与恢复 | 高级控制的范围、备份恢复及当前限制见 [实机入口说明](hardware/README.md)。逐台结论以运行目录本轮记录为准。任意波形尚未实机验收：已取得 SDG2000X 上传/读回依据，但未确认本型号的远程文件清理方法，验收工具不会上传或删除文件，不套用 SDG6000X 的删除命令。实际波形和输出精度未验收。 |

## 已知限制与待测

| 项目 | 边界与后续验证 |
| --- | --- |
| 当前限制 | 任意波形远程清理未经核实，实机入口不上传或删除文件；MAN/EXT 下不返回突发 PRD，周期未测。实际输出和精度未验收。 |
| 证据维护 | 逐接口、固件和测试条件核对运行目录的结构化资产状态；历史受阻不删除，缺项不提升为通过 |

## 扩展与移除

遵循[设备模块规范](../../../../../docs/architecture/device-modules.md)。新增操作须在本包提供型号依据、校验及测试；删除注册项和本包后重新生成文档。保留所有现场历史记录。

## 离线记录

本包 `hardware/records.py` 负责历史字段解释和统一摘要。新增批次保存 summary.json；原始结果与通信保留。离线总览及导入方法见[记录规范](../../../../../docs/testing/records.md)。
