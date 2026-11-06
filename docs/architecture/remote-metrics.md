# 远程指标处理与自动采集

指标处理复用桌面 Runtime、设备会话与 MCP。型号通过 `metric_profiles` 声明支持的处理项，客户端根据返回的指标值和质量执行试验判定。当前型号绑定见[生成矩阵](../generated/capability-matrix.md#mcp-指标处理)。

## 型号契约

在型号 `DEFINITION.metric_profiles` 引用 `MetricProfile`，通过唯一 `devices/catalog.py` 生效。根据型号实际可保存的测量数据选择共享指标，按物理量、单位、横轴和记录形态校验输入。测试示例见 [test_agent_metrics.py](../../tests/integration/test_agent_metrics.py)。

| 处理项 | 适用数据 |
| --- | --- |
| `voltage.waveform_statistics` v2 | 示波器单帧电压波形，样本 V、时轴 s，逐通道返回均值、峰峰值、含直流分量的 RMS 和总体标准差（`ddof=0`） |
| `optical_power.fluctuation`、`optical_power.reference_deviation` | 光功率计的 W 或 dBm 标量帧，计算前统一换算为 W。波动需至少两个读数，参考偏差按通道传入参考功率 |
| `spectrum.peak_and_snr` | 射频频谱仪的单条 dBm 扫频轨迹，频率轴为 Hz |
| `phase_noise.at_offset` | 相噪仪单帧 dBc/Hz 曲线，正偏频轴为 Hz |
| `temperature.stability` | 温控仪同一通道的温度标量序列。算法已实现，真实温控仪型号待接入 |

上述已注册处理项通过 `start_experiment` 或 `start_processing` 接收已保存记录。采集窗口由对应型号的采集接口定义，例如 RTM3004 的现有导出窗口和 DHO924 的 `screen` 模式。

每项定义包含稳定 `id`、算法 `version`、说明、继承 `MetricParameters` 的严格参数模型、命名指标及固定单位、`process(source, parameters)`。参数默认值展开后用于缓存身份；改变算法或算法所依赖的解释必须更新版本。所有声明指标都须返回 `MetricValue`：有限数值和质量；无法计算时为 `None/unavailable`，不填零或 NaN。最多 128 项指标，结果文件不超过 256 KiB，不返回曲线数组。模拟来源的有效数值始终标记 simulated。

声明 `per_channel_metrics=True` 的处理项将指标定义作为每通道的模板。服务端按已核验的实际通道展开指标名，形如 `ch9.fluctuation_percent`，并检查总指标数。客户端可从 `get_processing_capabilities` 的 `per_channel_metrics` 字段识别这类处理项。

`MetricInput` 只提供原始记录元数据、完整性、可重开帧迭代器、取消检查和进度回调。迭代器按原精度读取统一测量格式；处理器须有界读取并定期检查取消，纯 CPU 循环也要主动检查。处理器只消费测量帧及算法参数。通用算法可跨型号复用，型号专属数据解释与能力绑定留在型号包。

可选 `capture_parameters` 与 `capture` 必须同时声明。纯方案函数返回 `CapturePlan`，支持单帧 `single`、固定帧数 `count`，以及由型号现有 `run_acquisition` 管理结束的 `finite`；后者必须给定 duration。无限连续采集仍使用 `start_recording`、`stop_task` 后单独处理。开始自动流程前验证全部参数、采集/处理/结果权限及输出与初始化权限；不自动连接、设置额外参数或扩大授权。

## 接口与生命周期

| 工具 | 主要参数及行为 |
| --- | --- |
| `get_processing_capabilities` | `device_id`；提供 `grant_id` 时返回授权设备的处理项。临时命令开关开启时，省略 `grant_id` 可查询 `local.command`。 |
| `list_recordings` | `grant_id`，可选 `device_id`；返回 recording_id、类型、实际通道、帧数、完整性与可用性 |
| `start_processing` | `grant_id, device_id, request_id, recording_id, profile_id`，可选 parameters、allow_partial、force |
| `start_experiment` | `device_id, request_id, profile_id`，可选 `parameters`。普通处理项需要 `grant_id`，并用 `recording_ids` 或 `capture_parameters` 选输入。`local.command` 传 `recording_ids` 和 `command` 字符串。 |
| `list_processing_results` | `grant_id`，可按 device_id、recording_id、profile_id 过滤 |
| `get_processing_result` | `result_id`；普通指标结果还需要 `grant_id`。开关开启时可直接读回 `local.command` 结果。 |

提交返回原有 Agent 任务结构，任务标识字段是 `id`；后续传给 `get_task_status.task_id`。通用 state 沿用 accepted/running/stopping/completed/partial/failed/interrupted，并增加 cancelled；`phase` 区分 queued、acquiring、saving、validating、processing、publishing、completed 或 reused。progress 是处理器提供的 done/total，不假装是硬件采集百分比。completed 表示任务完成，不代表物理指标合格；partial 和每项 unavailable 必须由调用方单独判读。

`recording_ids` 接收 1 至 4096 个不同的记录 ID，与 `capture_parameters` 互斥。多个输入要求处理项声明 `supports_multiple_recordings=True`，记录均来自同一台授权设备且各含一帧。算法从 `MetricInput.frames()` 按提交顺序逐帧读取，并可用 `MetricInput.recording_ids` 获取完整来源。多条记录的结果以首条 `recording_id` 为索引，另存有序的 `recording_ids` 和包含全部记录内容的组合指纹。查询 `list_processing_results` 时按首条记录 ID 过滤。

一个指标线程、最多九个在途任务（一个执行和八个等待）。提交阶段只检查身份、参数、收尾摘要及文件版本快照；完整文件校验、实际通道授权和算法均在有界后台执行，返回任务 ID 不代表数据已通过验证。验证完成前任务可见性绑定提交授权的完整通道范围，之后收窄到源文件实际通道。同授权通道范围、同输入、同算法版本和有效参数的处理正在执行时，非 force 请求引用已有任务，并登记各 request_id 的防重关联。相同 request_id 不同参数拒绝。重复计算默认复用完整结果，force 另存新结果。若共享任务的原授权撤销，它会停止，加入者不会扩大其权限。

客户端断线不取消任务；`stop_task` 协作取消处理或停止本任务自有采集，并等待原始记录收尾，不停止其他设备任务。排队取消不会运行算法。软件退出等待已接受 I/O 和计算器协作退出；重启后未完成任务记为 interrupted，不重放。授权在运行期间无时间限制，撤销或重启后失效。

## 临时命令处理

在服务端“系统设置 → Agent 协作实验”勾选“启用临时命令处理”并应用。本机和已开放的实验网 HTTPS 的所有 MCP 客户端均可使用 `local.command`。它使用独立的命令任务范围，提交、查询、取消和读回结果时可省略 `grant_id`。采集、查询设备记录和运行普通指标处理使用实验授权。开关随软件退出复位。命令在采数机以桌面程序的 Windows 用户身份执行。

向 `start_experiment` 传入下列 `arguments`。同一个处理项适用于所有设备，命令直接由客户端提交。

```json
{
  "device_id": "<设备 ID>",
  "request_id": "<本次请求 UUID>",
  "profile_id": "local.command",
  "command": "python.exe \"process custom.py\"",
  "recording_ids": ["<记录 ID>"],
  "parameters": {"channel": 1}
}
```

`command` 是一行 Windows 命令字符串，最长 4096 字符。服务端按 Windows 引号规则拆分参数并直接启动程序。脚本和可执行文件须在采数机可访问，相对路径按软件运行目录解析。客户端先完成采集并取得 `recording_ids`，临时命令处理只接收已有记录。多条记录沿用同设备、单帧及提交顺序约定。

程序从 stdin 读取一个 UTF-8 JSON 对象 `{"source":{...},"parameters":{...}}`。其中 `source` 包含 `recording_id`、`recording_ids`、`metadata`、`complete` 和完整的 `frames` 数组。它沿用原 `process(source, parameters)` 的记录元数据和测量帧结构，数组序列化为 JSON 数组，复数序列化为 `{"real":...,"imag":...}`。算法参数按提交的 JSON 对象传递。

程序的 stdout 输出一个 UTF-8 JSON 指标对象。键为指标名，值包含 `value`、`unit` 和 `quality`。下面是脚本输出示例。

```json
{"metrics":{"ch1.std":{"value":0.0123,"unit":"V","quality":"good"}}}
```

日志写 stderr。非零退出、超时或 stdout 格式无效会使任务失败，错误见 `get_task_status`。命令执行上限为 300 秒，stdout 上限为 256 KiB。取消任务会终止命令进程。在同一 `request_id` 下沿用原任务，新请求重新执行命令。任务审计保存命令摘要。完成后用 `get_processing_result` 读取指标，模拟记录的有效值统一标为 `simulated`。

## 记录、授权与结果

规范记录必须有 recording.json 的版本、run_id、device_id、adapter_id 与完整厂家/型号/序列号；无法核验的旧记录不远程开放，也不根据目录名或当前槽位推断身份。本版无序列号的历史记录明确拒绝远程指标处理，即使旧连接流程允许本机确认资产。记录 ID 由 device_id 与 run_id 派生，重命名、移动及算法更新不改变；重复 ID 的多目录副本报冲突。

仅从本机已登记的数据目录检索，客户端用记录 ID 引用数据。查看和选择记录需要当前 view/results 授权。执行普通处理项另需 process，并检查源文件实际全部通道。自动采集还检查 acquire 或 record、必要的 output_trigger/acquisition_setup。process 默认不勾选。解析或校验失败标记不可用，未完成保存的记录拒绝处理，不完整来源仅在声明 supports_partial 且请求 allow_partial 时允许。

原始输入指纹包括文件内容、相对名称与大小，不以绝对位置或修改时间作为内容身份。处理前后复核，排队期间变化也拒绝；不把处理结果和自动 CSV 纳入原始输入。公共目录预约与桌面处理、重命名、移动、回收站操作互斥，批量记录按整个批次保护。

直接连续采集在 `acquisition-events.jsonl` 保留结束证据：是否达到声明结束条件、请求帧数、停止及错误。有限任务提前停止、失败和通信缺口不会因为已保存的帧本身有效而被视为完整；后续分步处理同样要求显式允许不完整记录。

小型结果保存在采集目录 `指标结果/<结果ID>.json`，包含 schema_version、记录/资产身份、实际通道、原始指纹、处理项/版本、有效参数、完成时间、来源完整性与质量、命名指标（value/unit/quality）。原子发布成功并登记索引后才报告结果；失败不覆盖已有结果。数据库只维护文件引用及校验索引，移动后从稳定记录 ID 更新位置。历史结果不因算法下线被删除；读取不要求仪器连接或处理器仍存在，也不隐式重新计算。

## 验证边界

软件回归使用 Mock 验证生产型号的注册、采集保存、指标计算、结果读取及 MCP 连接器，也用测试专用能力覆盖缓存、权限、取消和重启。真实设备验收按[留档规范](../testing/records.md)执行。
