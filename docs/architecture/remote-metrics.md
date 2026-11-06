# 远程指标处理与自动采集

本功能复用当前桌面 Runtime、设备会话与 MCP；不启动另一套硬件服务。全部生产型号的新增 `metric_profiles` 默认空，只有型号包显式声明后才支持指标处理或自动采数处理。现有基础采数接口不受影响。外部程序负责指标判读，公共层不提供猜测的算法或合格阈值。

## 型号契约

在型号 `DEFINITION.metric_profiles` 注册 `MetricProfile`，通过唯一 `devices/catalog.py` 生效。定义位于 [domain/metrics.py](../../src/lab_control/domain/metrics.py)，完整测试专用例子见 [test_agent_metrics.py](../../tests/integration/test_agent_metrics.py)；测试能力不注册到发行型号。

每项定义包含稳定 `id`、算法 `version`、说明、继承 `MetricParameters` 的严格参数模型、命名指标及固定单位、`process(source, parameters)`。参数默认值展开后用于缓存身份；改变算法或算法所依赖的解释必须更新版本。所有声明指标都须返回 `MetricValue`：有限数值和质量；无法计算时为 `None/unavailable`，不填零或 NaN。最多 128 项指标，结果文件不超过 256 KiB，不返回曲线数组。模拟来源的有效数值始终标记 simulated。

`MetricInput` 只提供原始记录元数据、完整性、可重开帧迭代器、取消检查和进度回调。迭代器按原精度读取统一测量格式；处理器须有界读取并定期检查取消，纯 CPU 循环也要主动检查。它不接收 Qt、Runtime 或仪器句柄，不创建全量 CSV/SQLite/PNG 副本；算法和型号专属解释留在型号包。

可选 `capture_parameters` 与 `capture` 必须同时声明。纯方案函数返回 `CapturePlan`，支持单帧 `single`、固定帧数 `count`，以及由型号现有 `run_acquisition` 管理结束的 `finite`；后者必须给定 duration。无限连续采集仍使用 `start_recording`、`stop_task` 后单独处理。开始自动流程前验证全部参数、采集/处理/结果权限及输出与初始化权限；不自动连接、设置额外参数或扩大授权。

## 接口与生命周期

| 工具 | 主要参数及行为 |
| --- | --- |
| `get_processing_capabilities` | `grant_id, device_id`；返回 supported、处理项、参数 JSON schema、指标及自动采集能力，不要求连接仪器 |
| `list_recordings` | `grant_id`，可选 `device_id`；返回 recording_id、类型、实际通道、帧数、完整性与可用性 |
| `start_processing` | `grant_id, device_id, request_id, recording_id, profile_id`，可选 parameters、allow_partial、force |
| `start_experiment` | `grant_id, device_id, request_id, profile_id`，可选 capture_parameters、parameters；有限采集成功收尾后处理 |
| `list_processing_results` | `grant_id`，可按 device_id、recording_id、profile_id 过滤 |
| `get_processing_result` | `grant_id, result_id`；直接读取已保存结果，无计算副作用 |

提交返回原有 Agent 任务结构，任务标识字段是 `id`；后续传给 `get_task_status.task_id`。通用 state 沿用 accepted/running/stopping/completed/partial/failed/interrupted，并增加 cancelled；`phase` 区分 queued、acquiring、saving、validating、processing、publishing、completed 或 reused。progress 是处理器提供的 done/total，不假装是硬件采集百分比。completed 表示任务完成，不代表物理指标合格；partial 和每项 unavailable 必须由调用方单独判读。

一个指标线程、最多九个在途任务（一个执行和八个等待）。提交阶段只检查身份、参数、收尾摘要及文件版本快照；完整文件校验、实际通道授权和算法均在有界后台执行，返回任务 ID 不代表数据已通过验证。验证完成前任务可见性绑定提交授权的完整通道范围，之后收窄到源文件实际通道。同授权通道范围、同输入、同算法版本和有效参数的处理正在执行时，非 force 请求引用已有任务，并登记各 request_id 的防重关联。相同 request_id 不同参数拒绝。重复计算默认复用完整结果，force 另存新结果。若共享任务的原授权撤销，它会停止，加入者不会扩大其权限。

客户端断线不取消任务；`stop_task` 协作取消处理或停止本任务自有采集，并等待原始记录收尾，不停止其他设备任务。排队取消不会运行算法。软件退出等待已接受 I/O 和计算器协作退出；重启后未完成任务记为 interrupted，不重放。授权沿用本机现有有效期和重启失效规则。

## 记录、授权与结果

规范记录必须有 recording.json 的版本、run_id、device_id、adapter_id 与完整厂家/型号/序列号；无法核验的旧记录不远程开放，也不根据目录名或当前槽位推断身份。本版无序列号的历史记录明确拒绝远程指标处理，即使旧连接流程允许本机确认资产。记录 ID 由 device_id 与 run_id 派生，重命名、移动及算法更新不改变；重复 ID 的多目录副本报冲突。

仅从本机已登记的数据目录检索，不接受远端路径。查看/选择记录需要当前 view/results 授权；执行另需 process，并检查源文件实际全部通道。自动采集还检查 acquire 或 record、必要的 output_trigger/acquisition_setup。process 默认不勾选，旧授权不自动增加权限。解析或校验失败标记不可用；未完成保存的记录拒绝处理，不完整来源仅在声明 supports_partial 且请求 allow_partial 时允许。

原始输入指纹包括文件内容、相对名称与大小，不以绝对位置或修改时间作为内容身份。处理前后复核，排队期间变化也拒绝；不把处理结果和自动 CSV 纳入原始输入。公共目录预约与桌面处理、重命名、移动、回收站操作互斥，批量记录按整个批次保护。

直接连续采集在 `acquisition-events.jsonl` 保留结束证据：是否达到声明结束条件、请求帧数、停止及错误。有限任务提前停止、失败和通信缺口不会因为已保存的帧本身有效而被视为完整；后续分步处理同样要求显式允许不完整记录。

小型结果保存在采集目录 `指标结果/<结果ID>.json`，包含 schema_version、记录/资产身份、实际通道、原始指纹、处理项/版本、有效参数、完成时间、来源完整性与质量、命名指标（value/unit/quality）。原子发布成功并登记索引后才报告结果；失败不覆盖已有结果。数据库只维护文件引用及校验索引，移动后从稳定记录 ID 更新位置。历史结果不因算法下线被删除；读取不要求仪器连接或处理器仍存在，也不隐式重新计算。

## 验证边界

公共回归只用临时 Mock 型号能力验证采集、计算、缓存、权限、取消、重启、文件占用、失败及 MCP/连接器，不代表生产型号具备算法，也不代表真实设备验收。新增型号算法需在其包内独立补充数值样本和测试；涉及真实设备按[留档规范](../testing/records.md)执行。
