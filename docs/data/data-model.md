# 数据模型与运行文件（0.3.0）

实机验证的原始批次与派生摘要遵循[记录规范](../testing/records.md)。`summary.json` 和 `overview/` 仅用于离线证据检索，不参与库存状态、会话或控制判断；原始通信、快照和恢复文件保留原样。

领域对象定义在 domain/instruments.py，持久化由 storage/repository.py 完成。新记录统一使用设置中的数据保存一级目录；新记录的工作区标识为 `general`，不增加目录层；旧接口仍接受 `electronic`、`optical` 元数据。历史 data/electronic 与 data/optical 保留原样。

`AcquisitionRequest.consistency` 支持 `full`、`live`、`stream`，默认 `full`；`stream` 需要本次运行的非空 `context_id` 且不能启动物理扫描。批次 `validation=cached` 表示曲线为新读取、配置沿用已有快照，`settings_timestamps` 保留原时间。连续记录缓存帧在 `performance.acquisition_context_id` 中带来源编号，监测缓存不具备记录来源；不能把后者当作完整记录帧。手动显示快照可导出监测缓存及 live 帧，但保留原校验级别、时间及配置来源，不升级为 full。`_session_configuration_version` 和 `monitor_*` 是软件调度元数据，不是仪器读回。历史文件保持原样。

`AcquisitionRequest.duration`（秒）和 `sample_rate`（samples/s）为可选字段，给定时必须为有限正数；旧请求省略后保持原行为。有限任务还需由型号校验档位、时长与点数上限，未声明支持的适配器明确拒绝。它们不表示界面刷新或写盘间隔。公共会话通过型号的 `run_acquisition(request, stop)` 接收有效批次，逐帧保存，禁止同时指定 `count` 或抽帧保存间隔；收尾语义见[监测一致性规则](../architecture/live-acquisition.md)。型号专属的完成标记、实际样本数、坐标来源与统计方法随配置/元数据保存，定义留在型号包。

会话快照 `controlsLocked` 只表示本软件连续采集/记录期间的参数锁，包含有限任务及二进制收尾，纯监测为否；不代表仪器物理面板被锁定。后台 CSV 转换不延长该锁。`acquisitionPreparation` 和运行日志记录可选二进制初始化的前后格式、字节序、写入是否尝试及成功/失败状态；不会删除失败后的最后可确认状态。

## 实际存储结构

    %LOCALAPPDATA%/TF-LabControl/       # LAB_CONTROL_HOME 可覆盖
      config/                         # 用户自行保存的 UTF-8 TOML
      inventory.sqlite3               # WAL 模式
      logs/YYYY-MM-DD.jsonl           # UTF-8 审计
      data/                           # 未配置时的新记录根目录
      reports/<UUID>.json
      simulation/                     # --mock 独立存储

用户选择的数据保存一级目录（默认是运行目录下的 data）：

```text
一级目录/
  单设备采集/YYYY-MM-DD/
    HH-mm-ss-SSS_型号_序列号_continuous_批次标识/
      记录说明.txt
      数据_00001.csv
      帧索引.csv                 # 连续数组数据需要
      _raw/
        recording.json
        segment-00001.sqlite3
        summary.json
        analysis-checkpoint.json
    HH-mm-ss-SSS_型号_序列号_single_frame_批次标识/
      记录说明.txt
      数据_00001.csv
      _raw/
        recording.json
        snapshot.json
        samples.npz
  批量采集/YYYY-MM-DD/HH-mm-ss-SSS_batch_批次标识/
    型号_序列号_采集类型_记录标识/
```

日期使用接收操作时的电脑本地日期，同一天共用日期层。每次记录独立创建末级目录，时间精确到毫秒，随机标识防止重名覆盖。型号及序列号清理 Windows 非法字符、保留名及尾部空格/句点；缺失时使用适配器、稳定设备 ID 或明确的未知占位。原始身份仍完整保存在 recording.json。

storage/paths.py 的不可变 `SaveContext` 固定根目录、带时区开始时间、身份、设备 ID、适配器、模式、类型和记录标识。`BatchSaveContext` 预留共用根目录、开始时间及批次路径；仅显式批量任务传入它，分别启动的多台设备仍属于单设备采集。本版不新增批量采集界面。构造上下文不创建目录或曲线文件。

`Repository.export()`、`create_recording()` 和 `Recording` 可选接收 `context`；未传入时仍支持原调用方式并生成新目录。仓储接口省略 `mode` 时使用上下文的标签，没有上下文则默认 `general`；显式标签与上下文不一致仍拒绝。界面保存当前帧、兼容的新读帧导出、连续记录及 CLI 在接受操作时建立上下文，再排队读取/写盘。修改根目录、跨午夜和后台 CSV 转换都不会改变已固定路径；单帧内容保留原帧时间及配置来源。

连续记录开始时立即独占创建目录、中文记录说明及 `_raw/recording.json`。清单版本 1 保存 `kind`、`mode`、`scope`、`run_id`、可选 `batch_id`、`device_id`、`adapter_id`、原始 `identity`、`started_local` 与 `started_utc`；新增 `layout_version=2` 表示分析文件在顶层、原始文件在 `_raw`。不改变 SQLite 格式版本。缺少布局版本的历史目录按版本 1 读取；保存上下文可显式使用旧布局。准备失败明确报错并撤销记录状态及参数锁，不切换备用位置。无有效帧即停止仍保留说明、清单和零帧摘要，不产生曲线 CSV 或分段文件。

自定义目录只影响采集数据。真实与 Mock 的偏好分别保存在各自库存中；Mock 默认使用 simulation/data，自定义一级目录下则额外使用 simulation 子目录，设置页展示实际生效路径。不迁移库存、槽位、日志、配置、实机档案或历史采集文件。

进程锁固定在每用户 TF-LabControl 根目录，防止换数据目录后启动第二个真实设备进程。`hardware.lock.owner.json` 只保存当前独占锁的 PID 和启动时间用于冲突提示，正常退出时删除，不能据此判断锁是否可用。SQLite 包含 inventory(id, document)、scans(id, document) 和新增 workspace(id, document)，document 为 UTF-8 JSON。新增表不改变旧库存/槽位格式；会话驻留内存，旧记录不能当成当前已连接。

## 页面工作区

workspace 使用键隔离，真实与模拟分别位于各自根目录：

- `current_mode`：正式界面固定为 `general`；原选择保存在迁移记录中。
- `data_storage/root`：用户选择的数据保存一级目录的绝对路径；缺省或 null 使用本运行目录的 data。保存前进行创建及写入/读回检查，失败保留原设置。
- `monitor_overview/all`：保存所有已连接可监测设备总览卡片的 `{enabled, interval}`。槽位存在时使用 `slot:general:<slot>`，未绑定槽位使用稳定设备 ID；不保存列数、仪器句柄或运行状态。采样间隔同时写入并以 `monitor-timing/general/<device>` 作为设备级 canonical 设置，设备页和总览修改后双向同步；旧的 `monitor_overview/electronic` 与 `monitor_overview/optical` 在启动时一次性兼容迁移。
- `monitoring_channels/optical_power_meter`：按光功率计功能槽位保存通道勾选列表；未绑定槽位使用稳定设备 ID。
- `draft/<mode>/<device_id>/<channel>/<operation>`：`fields` 保存原输入值及单位倍率，`rows` 保存数组草稿；成功应用后清除该项，失败保留。
- `demo_general_slots_initialized`：仅模拟入口首次初始化通用 A/B，避免再次启动覆盖用户绑定；原 `demo_slots_initialized` 键保留。

总览恢复不自动采样；列数根据当前内容宽度重新计算。只有当前已连接且存在数据源的设备显示卡片。

## 库存与资源候选

库存记录包括 id、displayName、identity、identityKey、adapterId、resources、config（主资源）、slots、candidates、discoveryState、driverStatus、lastSeenAt、lastProbeAt、lastError、probeReason 和 scanRunId。

candidates 以原始资源字符串为键，保留 candidateId、scanRunId、source、rawDescriptor、lastSeenAt、config、lastError、probeReason。同一物理设备的多个地址合并后不删除候选；合并前的空身份占位行可被吸收。存在冲突时保留原身份和 identityConflict，连接被禁用，不用新身份覆盖旧资产。

库存及候选新增 presenceState（present/not_seen/unreachable/unknown）；候选的 evidenceScanRunId 标识存在性证据批次，sources 按来源保留 scanRunId、observedAt、rawDescriptor。旧单来源候选在下次合并时兼容补入 sources，无需重建数据库。configured 的 observedAt 只表示配置被枚举，不刷新 lastSeenAt；真实本地枚举或身份成功才更新后者。仅 `metadata_only` 且没有型号/适配器的串口资源保留为待核对线索，不进入当前设备集合。对已有型号或身份记录，串口/USB 枚举后的身份探测失败保持 unreachable，不因历史身份进入当前设备集合。启动时仅在内存中将旧存在性置为 unknown，并去除旧 evidenceScanRunId，等待当前扫描/会话确认。

稳定 id 是持久化 UUID；可识别身份以厂家/型号/序列号匹配历史记录。序列号缺失时保持候选身份，不能因 COM/IP 相同证明是同一台设备。显示名与槽位保留。连接后才发现历史身份重复时保留 conflict，不在运行 worker 中偷偷更换设备 ID。

型号协议无法读出序列号时，明确选择的适配器和资源仍可并行建立会话；端口、别名和配置中的预期序列号分别承担占用隔离。预期序列号只作为用户资产标签和租约键，不能替代设备实际身份读回。

`slots` 使用通用键，例如 `{"general": "signal_generator:1"}`；旧 `electronic`、`optical` 键保留原样。缺少键代表尚无绑定记录，空字符串代表明确未绑定。身份合并不得用不同的历史选择覆盖显式空绑定；相互矛盾时保留冲突。同型号不同序列号不会继承槽位。无需新增表或重建旧库存。

`bind_slot(device_id, slot, mode, replace_device_id=...)` 的可选参数表示用户已确认替换的当前占用者；省略时占用仍报错，占用者变化时报 `slot_changed`，须重新确认。两台设备的任务状态和库存占用在同一锁定区间核对，仓储 `save_many()` 用一个 SQLite 事务提交双方记录，成功后才替换内存记录。事务失败不发布半完成状态；`slot_changed` 审计包含被替换设备 ID。

批量连接进度仅在内存保存，包含状态、总数、已处理数、当前设备和逐项结果；结果状态为 `connected`、`already_connected`、`failed` 或 `skipped`，附设备 ID、显示身份和原因。返回的进度与最终结果是独立快照；逐项结果及汇总写入运行目录审计日志。它们不改变库存格式、槽位或实机验收结论。

## 会话快照

snapshots 返回 stableDeviceId、sessionId、resource、adapterId、connectionState、activeTask、pending、lastStatus、lastError、identity。lastStatus 包含状态值和 UTC 时间。

0.3.0 增加 monitoring、recording、configurationVersion、controlPending。监测任务存在时会话持续 busy，批次间也不会误允许断开。recording 在在途记录或写盘尚未结束时继续为真，用于参数锁。控制前值来自已读回/已接受缓存，并写入 operation_requested.before；没有历史值时为 null，不虚构。

发现状态：unknown/identified/stale/unreachable/conflict。identified 要求当前存在证据与身份记录同时成立；仅保存历史 identity 不代表本次已发现。candidate 与 duplicate 是设计概念，本版候选通过 candidates 集合保存，重复资源通过合并或 conflict 表达，不增加虚假的连接状态。连接状态：connecting/connected/busy/degraded/error；没有会话即 disconnected。

扫描失败、取消或部分 provider 失败不覆盖连接快照。只有完整成功覆盖的发现来源才能把未出现的资源标记 stale。configured 地址表示配置存在，不代表网络设备在线。

## 扫描记录

ScanRun 保存 id、startedAt、finishedAt、duration、state、providers 和 items。每个 provider 有数量、成功/错误及错误原因；每项有 device_id、resource、reason、error。开始、结束、provider、探测/缓存/跳过均写审计。

## 测量与质量

`MeasurementBatch.timing` 为可选对象；缺失代表历史记录未保存这些信息。版本 1 包含 `host_read_started_at`、`host_read_completed_at`（带时区的 UTC ISO8601）、`read_duration_seconds`（单调时钟耗时）与 `clock_source`；记录器添加 `recording_elapsed_seconds`，以本次记录创建时的单调时钟为起点。`timestamp` 对公共会话采集表示读取及校验完成时间，`time_basis` 明确这个含义。它不是物理触发时间，也不是写盘时间。系统时钟回拨可导致绝对 UTC 倒退，但不影响实际耗时和记录相对时间。

通道 metadata 的 `received_at` 在波形回包收齐时捕获，`received_time_source` 为 `host_utc_at_waveform_reply`。原生 `trigger_time_local` 单独保存；LeCroy 标记 `instrument_local_timezone_unknown`，不能直接转换 UTC；DHO924 未提供绝对触发时间，`trigger_time_source=unavailable`。波形 axis 保留仪器实际返回的相对坐标及单位，不使用主机帧间隔生成。

JSON 与版本 1 SQLite 的文档字段保留完整 timing，无须变更表结构；旧记录缺失字段不推算。NPZ 保留原数组键，增加无需 pickle 的 Unicode 标量 `metadata_json`，保存帧与通道元数据。旧布局的详细 CSV 版本 2 在原列之后增加 `csv_format_version`、读取开始/完成/耗时、记录相对时间、通道接收时间、触发时间及各自来源和 `time_basis`；没有版本的旧检查点继续原列布局，不混写。新版布局只自动生成下述分析 CSV，不再重复生成详细 CSV。历史缺失时间为空，保存已显示帧不重新打时间戳。

Measurement 保存 quantity、unit、values、channel、axis、axis_unit、quality 和 metadata。MeasurementBatch 保存 identity、measurements、configuration、timestamp、time_basis、adapter_version 及可选 timing。

ParameterReading 保存 operation、channel、value、source、timestamp、quality、error。MonitorFrame 保存 sequence、configuration_version、request、batch；后台每个数据源保留最近一批，显示层标量趋势限制为 1200 点/600 秒。配置快照新增 `_session_configuration_version`；显式监测记录另含 `monitor_record_session` 和 `monitor_request`。界面抽稀不改变导出数组。

- 标量可以有一个或多个读数；波形和频谱包含对应坐标轴。
- 复数 S 参数在 NPZ 保留 complex dtype；JSON 分 real/imag，CSV 分实部/虚部列。
- snapshot.json 保留身份、设置缓存、传输/前导信息和质量。配置缓存只记录本会话已接受设置及主动读到的元数据，不宣称是设备全量状态快照。
- UTC 时间为主机读回时间；时轴来自仪器前导数据或明确标注的 SDK/TSERVE 周期，不代表跨设备硬件同步。
- quality 为 good/simulated/timeout/disconnected/parse_error/overload/sensor_missing/stale。故障批次使用空数组与错误元数据，不使用零值补齐。非有限 JSON 数字转 null。
- 流式任务先前有效批次保留；中断时审计错误及已提交路径，不生成冒充有效帧的空数据文件。

NPZ 使用 NumPy 数据数组，不保存 pickle 对象。CSV 使用 UTF-8 BOM 便于中文 Excel 打开。Touchstone 要求 S11 或完整同一频率轴 S11/S21/S12/S22，顺序按规范，参考阻抗显式传入；不虚构缺失 S 参数。

### 连续记录格式 v1

新版连续记录的 `_raw` 包含 segment-00001.sqlite3 等分段文件、转换检查点和状态，以及停止时的 summary.json。SQLite user_version=1：configs 保存配置和原始设置时间，按 SHA256 内容版本去重；frames 保存递增序号、配置版本引用、时间、身份、质量验证级别和性能；samples 保存各通道元数据、dtype、shape 及 values/axis BLOB，保留复数、字节序和精度，不使用 pickle。一个帧的所有通道和配置引用在同一事务提交，转换进程只读取已提交帧。

旧布局连续 CSV 含 sequence、config_id 列，仍以 UTF-8 和原检查点恢复；旧单帧 CSV 使用 BOM。可离线执行 `python -m lab_control.storage.recording --csv <记录目录>` 恢复转换，按布局版本选择分析或旧详细 CSV，不连接仪器。read_frames 继续接受分段文件路径；历史 JSON/CSV/NPZ 文件不迁移、不改写。

录制摘要及会话状态区分 received、pending、committed、converted、paused、error、csv_error；已收到不等于已落盘，CSV 转换失败不修改二进制提交数。start_acquisition 仍返回路径列表，路径单位改为分段文件，实际帧数见摘要。MonitorFrame 增加单调时钟 received_at 用于显示延迟，不用它替代 UTC 数据时间。

### 面向 Excel／Origin 的分析 CSV

所有分析 CSV 使用 UTF-8 BOM、中文首行表头和带单位列名；不按界面精度舍入数值。`数据_00001.csv` 等文件跨原始数据库分段追加，每文件最多 100 万数据行或 100 MiB（含 BOM 与表头），优先在帧间轮转；超大帧跨文件时保留帧号和连续点序号。通道、物理量、单位及列结构变化开启新文件，点数和横轴数值变化不改变列结构。

| 数据 | 列组织 |
| --- | --- |
| 标量、电源、光功率、参数状态 | 一次读回一行；本地时间、距记录开始秒数、各测量列、可用来源及状态 |
| 波形、频谱、相噪、稳定度 | 帧号、点序号、各通道自己的真实 X/Y；频率、时间、偏移频率、平均时间分别标注 |
| 复数 | 横轴、实部、虚部、线性幅值、相位；零幅值相位留空，不猜测 S 参数名称 |
| 无逐点时间的多读数批次 | 帧号、点序号、读数；不按软件间隔补出采样时间 |

多通道按点序号并排，点数不同的位置留空，不插值、不补零，也不表示同步。数组按原顺序展平，原形状留在原始记录。未知物理量保留其原名称；状态文本按文本输出，防止电子表格当作公式执行。

连续数组的 `帧索引.csv` 按帧及跨文件片段记录数据文件、起止逻辑行（含表头）、起止点、本地读回时间、相对秒数、读取耗时、质量、配置版本、可用的通道接收及仪器触发时间、来源。点序号从 1 开始。索引本身达到 100 万行／100 MiB 时续写 `帧索引_00002.csv` 等文件。标量不重复生成索引。单帧的原帧时间和本次保存时间写入记录说明，完整时间仍保留在原始 JSON／NPZ。

本地时间显示为 `2026-09-28 14:30:06.456789`，表头注明从清单 started_local 固定的 UTC 偏移；历史未提供偏移使用 UTC，无法解析的时间留空。相对时间只使用原始 timing 中的单调时钟值，不从帧号或间隔推算。主机、仪器触发时间和曲线横轴不是同一个时间基准。

分析导出通过磁盘暂存逐帧分块写出，不将整段 CSV 放入内存；所有数据文件和帧索引 fsync 后共同提交 `_raw/analysis-checkpoint.json`。意图清单只跟踪本导出器创建的文件，中断恢复截断未提交尾部、清理未提交新文件，不覆盖用户同名文件。每个目录仅允许一个转换器写入。分析错误与原始写盘错误分别报告，停止后的参数锁不等待 CSV。

`export_analysis(source, destination)` 将旧／新原始记录离线导出到独立新目录，输出目录不得存在或与来源嵌套。导出只读取来源；输出说明注明原始数据所在位置，不复制原始曲线。CLI：`lab-control-cli export-analysis <来源目录> --output <新的分析目录>`。默认新记录自动生成分析 CSV，无需手动调用此接口。

## 审计和报告

审计记录连接参数、目标身份、操作请求/结果、步骤数、失败与清理事件；流任务记录开始/停止和错误。报告含测试范围、目标、时间、请求/读回、数据路径及 hardware_accepted=false。报告成功只代表执行了该次检查，不自动提升设备验证等级。

HDF5、高频无限流写盘、完整实验/阈值索引、遥测和科学数据解析仍为后续设计。附件载荷帧格式为空，本轮没有猜测数据包定义。

## 通用工作区迁移

运行时取得进程锁后、加载内存库存前，在同一个 SQLite 事务中执行 `workspace_migration/general-v1`。只补充缺失的通用设置，保留全部旧键、原始批次和历史数据文件；事务失败则全部回滚，下次启动可重试。迁移完成标记使后续启动不覆盖用户的新设置。

光谱仪／光功率计优先使用旧 optical 设置，频谱仪优先 electronic，其他设备优先原 current_mode（缺失时 electronic）；只有键不存在才尝试另一模式，显式 null、空列表和空绑定均保留。复制参数草稿、采样间隔、软件标记、示波器微调、总览偏好和光功率计通道选择。各类别每次启动的采样间隔重置规则继续生效。

无歧义旧槽位复制到 general。同一设备旧绑定不一致、槽位类别不匹配或多设备争用同一槽位时，general 保持未绑定，库存 slotMigrationConflict 保存提示，重新绑定／解除绑定会清除此提示；原冲突依据保存在迁移记录中。既有 general 绑定和设置优先，不被旧值覆盖。未绑定设备的总览偏好使用稳定设备 ID，避免冲突槽位混用。

监测、采集、记录、保存上下文和 CLI 统一接受 general/electronic/optical；默认使用 general，显式旧标签仅影响元数据，未知标签仍拒绝。保存目录及分段版本不变，旧记录读取和离线转换无需改写。
