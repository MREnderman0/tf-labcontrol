<!-- Generated from an independent device package; edit its README.md. -->

维护源：[包内 README](../../../src/lab_control/instruments/devices/lecroy_waverunner9404m/README.md)。修改维护源后重新生成。

# Teledyne LeCroy WaveRunner 9404M

独立注册标识 `lecroy.9404m`；精确匹配 LECROY/TELEDYNE 的 WaveRunner9404M 或 9404M，物理通道 C1–C4。本目录是型号维护源。

## 能力与协议

保留迁移前十四项基础控制：显示开关、档位、偏置、耦合、探头倍率、触发电平/边沿、时基、水平位置、触发模式、存储长度、运行、停止、单次。增加通信格式与关联模式的只读查询。型号适配器覆盖控制前置校验与完整波形采集，不调用旧混合示波器实现。

紧凑监视新增 `trigger_source`：通过 `TRSE EDGE,SR,Cn` 仅改变模拟通道边沿触发源，读写前后解析完整 TRSE，保留并核对保持条件。高级触发或外部源保持只读；新增源切换目前仅通过协议夹具，待实机验证，不包含在历史基础控制通过结论内。

协议依据：[MAUI Remote Control and Automation Manual，February 2026](https://cdn.teledynelecroy.com/files/manuals/maui-remote-control-and-automation-manual.pdf)，第 6 部分波形描述/缩放，第 7 部分 CHDR/CFMT/CORD/WFSU、通道及触发命令；范围依据：[9404M 官方规格](https://www.teledynelecroy.com/options/productdetails.aspx?categoryid=1&groupid=5&modelid=11110)。协议契约与资产实测结论分开。

## 连接与驱动

默认 PyVISA + Windows x64 VISA，使用当前扫描返回的 VXI-11 INSTR 资源。仪器需在 Utilities → Utilities Setup → Remote 选择 **LXI (VXI-11)**。TCP/IP 是 VICP 模式，VXI-11 的身份查询可能仍成功，但其他查询返回接口警告；适配器报 `remote_interface_mismatch`，不将警告当参数、不自动改接口或重放命令。

`deployment_components=("rs-visa",)`，复用[部署组件清单](../../../deployment/components.json)中的现有来源、材料哈希、检测和待补分发依据。无需新 SDK，不修改 DHCP、防火墙或系统代理、不自动重启。Windows 10 仪器端的 LXI 管理员运行条件见厂家手册。USB 和 VICP 保留旧配置路由兼容，但尚未取得本次资产接口验收结论。

## 单位与限制

电压 V、时基 s/div、位置 s、倍率 ratio、存储长度 SAMPLE；标量兼容响应头与科学计数法，区分 MAUI 的 M（毫）和 MA（兆），未知单位、非有限值和多回包均拒绝。扫描与显式连接共用身份解析，去除 CHDR 添加的 `*IDN` 响应头后计算厂家/型号/序列号键，同时保留原始回复供留档。旧扫描曾将响应头误记为厂家而造成重复资产与假冲突；这类历史记录需在备份及真实身份核验后修复，不能通过忽略序列号或停用冲突检查解决。

迁移前静态范围保留为软件边界，不代表完整仪器极限。连接、主动刷新及关联配置改变后，通过现有会话读取 MAUI CVAR 的 GetMinValue/GetMaxValue/GetGrainValue，与软件边界取交集；稳定帧没有范围查询。获取失败时能力标为过期，禁止写入。CVAR 未读取的离线协议路径保留原有保守范围。时基范围可能随仪器当前配置改变，不能将旧 1000 s 静态上限当作实机可设值。

设置只发送一次，再用一次有界 `*OPC?` 同步后读回，VISA 完成等待最多为连接超时与 30 秒中的较大值（总上限 60 秒），超时关闭句柄并要求重连；兼容 `1` 与 `*OPC 1` 响应。偏差仅在 CVAR 请求/适配值一致且满足粒度半步或最近粗调档位规则时接受，结果保留适配证据。100 ms 请求读回 20 ms 等无法解释的差异仍拒绝；不修改公共数值容差。SINGLE/运行等触发状态不使用完成查询来宣称新波形已经取得。

界面滑块固定为缓存的合法端点，使用 1–2–5 粗调档位并保留已有细调值；偏移/电平按仪器粒度键盘微调，不以 UI 档位冒充硬件唯一取值。水平位置仍核对时基关联约束。依据[厂家操作手册](https://cdn.teledynelecroy.com/files/manuals/waverunner9000-om-eng.pdf)的通道/时基设置：正 OFST 将波形向上移，正 TRDL 将触发点向右移。自动化对象路径的参考见[厂家 WaveRunner Automation Reference](https://cdn.teledynelecroy.com/files/manuals/automation_command_ref_manual_wr.pdf)，实际范围以本型号 CVAR 返回为准。

## 采集与监测

连接身份与资源锁核对后，`prepare_acquisition()` 先备份 CHDR/CFMT/CORD/WFSU，必要时仅设置 `CFMT DEF9,WORD,BIN`，核对前后状态。保留响应头、字节序及传输窗口，不逐帧改格式。正常断开不恢复格式；硬件测试显式恢复。扫描和只读预检必须用 `configure_acquisition=False`。

仅接受 C1–C4 普通单段波形、已知 LECROY_2_3/2_4 描述，校验完整定长块、长度/点数、通道来源、字节序、有效区间、有限值、缩放与时间轴。分段、RIS、数学/多数组及未知描述明确拒绝。描述中的单位与坐标是数据来源；不以请求点数冒充实际长度。当前最多处理 1000 万点，受公共消息上限约束。

“采集点数”直接设置 MSIZ 最大采集长度，范围为 CVAR 与软件 1,000～10,000,000 的交集；AcquisitionRequest.points 仍是兼容请求提示。采集前检查存储与解码预算，超限提示调整点数。VISA INSTR 波形采用有界定长块读取，先检查声明长度，超限、截断和尾随数据分别记录声明/接收/限制字节数；边界不可靠时关闭连接，不能截断后复用。即使停止时仍留有旧大波形，也由接收预算拦截，不降低 WORD 精度或悄悄抽点。

连接、主动刷新及关联参数改变时另读取 SmartMemory、NumPoints 和 SamplingRate；它们不进入稳定帧查询。厂家区分 SetMaximumMemory 与 FixedSampleRate。后者保持采样率，实测 MSIZ 读回成功也可能仍产生 6400 万点；预算同时核对实际 NumPoints。当前不自动切换内存模式，固定采样率模式下提示调整时基或仪器内存模式，不能将 MSIZ 成功当作实收点数已受限。修改前面板后需主动刷新，接收长度上限仍独立生效。

`full` 在波形前后完整读取关联设置；`live/stream` 每轮首帧完整核验，稳定帧每通道仅查询一次 `WF? ALL`，保留完整 WAVEDESC 和 WORD 样本。缓存以运行标识、模式和所选通道绑定，保存原始参数时间；缓存帧标为 `cached`，不更新公共参数缓存时间。软件调参、主动刷新、重连、通道或运行变化、异常均使缓存失效。监测/记录期间没有独立参数轮询；空闲沿用公共当前设备 30 秒、其他设备 60 秒错峰同步。

每帧按自己的描述缩放。已验证描述字段与本轮基线有差异时报 `configuration_changed`；解析、超时、断线保持各自错误。无效帧不显示或保存。正常触发时间和水平起点抖动不触发完整重读；描述无法表达的前面板修改需要主动刷新。仅接受 FIRST_POINT=0、SPARSING_FACTOR=0/1 的完整窗口，不推测抽取窗口坐标。缓存字段对应与限制见[通信优化说明](../../../src/lab_control/instruments/devices/lecroy_waverunner9404m/communication-research.md)。

每通道保留实际接收时间及描述中的采集时间标识；运行时各通道顺序读取，不宣称同次触发。读取帧、可辨认的新采集及重复比例分别统计，保存不按时间标识去重。公共暂态期限、调度及记录参数锁见[采集规则](../../architecture/live-acquisition.md)。界面保存当前帧沿用已显示的不可变完整数据与原校验来源，不再读取仪器；兼容的新读帧导出 API 保留。连续记录在二进制排空后释放锁，CSV 不占仪器。

每个有效通道增加可选 `scope_display` 元数据：快照 VDIV、参考电压 −OFST、秒/格 TDIV、时间中心 −TRDL。公共网格独立映射各通道，但保存仍使用 WAVEDESC 解码的真实 V/s 数组。缓存帧的显示信息保留原始快照时间，不增加稳定帧查询。前面板改动须主动刷新；新版界面与网格仅做离线验证。

单次启动最多等连接超时与 5 秒中的较小值，未触发即报等待超时，不重发、不把 OPC 或旧波形当成新采集完成。软件监测默认被动读取；运行/停止/单次为独立显式控制动作。

## 文件与公共依赖

`profile.py` 定义协议与能力，`protocol.py` 负责标量及二进制验证，`adapter.py` 负责关联校验及采集；`tests/fixtures/capabilities-before.json` 是不可覆盖的迁移证据，`capabilities-current.json` 记录新增诊断查询与修正后的契约。专属控制夹具迁入本包。公共层仅依赖会话、传输、领域模型与存储，不交叉导入其他型号。

## 测试入口与实机状态

所有资产按[统一记录规范](../../testing/records.md)维护当前结论、接口矩阵、功能覆盖、已知问题及批次索引；补测更新对应表格，历史批次不改写。



| 项目 | 验证范围、条件及限制 |
| --- | --- |
| 入口与条件 | 软件：`.venv-dev/Scripts/python.exe -m pytest src/lab_control/instruments/devices/lecroy_waverunner9404m/tests -q`。 |
| 入口与条件 | 硬件：[测试入口与计划](../../../src/lab_control/instruments/devices/lecroy_waverunner9404m/hardware/README.md)。仅显式指定配置和预期序列号执行；默认只读，写入必须追加 `--write --empty-inputs`。所有测试通过正式型号工厂和会话执行。身份、参数、恢复、重连、波形与物理精度分别报告；当前资产证据以运行目录为准，不把 Mock 或可枚举当成全机通过。 |
| 历史验证范围 | 2026-09-25，固件 9.2.0，空输入 LAN/VXI-11：四通道基础参数及全局控制、运行/停止、SINGLE 等待状态、三次重连、WORD/LO 波形、单次保存和约 30 秒监测记录已分别实测。四通道各取得 5002 点；记录接收、二进制提交及 CSV 转换均为 19 组，停止后无待写入，参数锁释放。每帧完整核验带来的耗时未作性能达标声明。两个写入批次均恢复全部快照，差异为零。 |
| 效率验证 | 2026-09-25 后续三轮基准通过，默认启用 live/stream 缓存；稳定四通道 82→4 次查询，STOP 中位采集耗时约 429–474→20–24 ms。AUTO 监视/记录各 60 秒通过，另以真实保存逐点核对 220,088 行二进制/CSV。完整结果与条件见[通信优化说明](../../../src/lab_control/instruments/devices/lecroy_waverunner9404m/communication-research.md)。成组启动快照仅在已验证的固件/响应头下启用，读取块保留 VISA 默认。 |
| 入口与条件 | 首个控制批次的单次保存因测试入口漏传保存模式而中止，随后修复入口、增加跨层回归并另建采集保存批次通过；失败批次保留。早期 VXI-11 接口警告与 VICP 连接超时也保留独立证据；后来 VXI-11 参数读取已可用，未由软件切换仪器接口。USB、VICP 协议功能、物理精度、分段及高级分析尚未验证。 |

## 已知限制与待测

| 项目 | 边界与后续验证 |
| --- | --- |
| 当前限制 | 空输入采集不证明物理精度或带宽；USB、VICP 协议功能、分段及高级分析尚未验证。历史失败及后续独立恢复分别保留。 |
| 调参连续性复核 | 2026-09-26，空输入实测垂直偏移 60 μV→100 μV、水平位置 1.73 μs→1.74 μs，CVAR 证据一致且监测续采；点数设置与读回通过，但固定采样率模式未限制实收长度。20 ms/格下约 128 MB 回包在读取 64 字节后拒绝，后续测试停止。首批完成等待超时及恢复偏差保留；独立恢复批次最终逐项一致、差异为零。详见[本轮验收记录](../../reviews/oscilloscope-continuity-review.md)。 |
| 新增显示与源切换 | 固定网格、滑块、通道联动与触发源变更通过软件/协议夹具验证；触发源切换尚未实机复核，不能以偏移短测代替。 |
| 证据维护 | 逐接口、固件和测试条件核对运行目录的结构化资产状态；历史受阻不删除，缺项不提升为通过 |

## 扩展与移除

仅在本包扩展协议和测试，经 `devices/catalog.py` 注册并生成派生文档。移除时验证其他型号和旧配置的不可用提示，保留用户库存、配置和实机历史。WaveRunner 9254 保持既有实现，不依赖本包。

## 注册能力（自动生成）

范围、单位及命令来自当前注册定义；资产级实测结果见运行目录。

| 操作 | 参数 / 单位 / 范围 | 作用域 | 方向 | 额外确认 | 状态 |
| --- | --- | --- | --- | --- | --- |
| `channel_enabled` | value: enum  ON/OFF | 通道 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `scale` | value: number V/div [0.001, 10] | 通道 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `offset` | value: number V [-10, 10] | 通道 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `coupling` | value: enum  A1M/D1M/D50/GND | 通道 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `probe_attenuation` | value: number ratio [1, 10000] | 通道 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `trigger_level` | value: number V [-10, 10] | 通道 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `trigger_slope` | value: enum  POS/NEG | 通道 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `timebase` | value: number s/div [1e-10, 1000] | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `time_position` | value: number s [-100, 100] | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `trigger_mode` | value: enum  AUTO/NORM/SINGLE/STOP | 整机 | 读/写 | 需要 | 软件已实现；实测按资产查证 |
| `record_length` | value: integer  [1000, 10000000] | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `single` | 无 | 整机 | /写 | 需要 | 软件已实现；实测按资产查证 |
| `run` | 无 | 整机 | /写 | 需要 | 软件已实现；实测按资产查证 |
| `stop` | 无 | 整机 | /写 | 需要 | 软件已实现；实测按资产查证 |
| `transfer_format` | 无 | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `response_header` | 无 | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `byte_order` | 无 | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `waveform_setup` | 无 | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `trigger_setup` | 无 | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `sequence_setup` | 无 | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `interleaved` | 无 | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `trigger_source` | value: enum  C1/C2/C3/C4 | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
