# 数据模型与运行文件（0.3.0）

实机验证的原始批次与派生摘要遵循[记录规范](../testing/records.md)。`summary.json` 和 `overview/` 仅用于离线证据检索，不参与库存状态、会话或控制判断；原始通信、快照和恢复文件保留原样。

领域对象定义在 domain/instruments.py，持久化由 storage/repository.py 完成。新记录统一使用设置中的数据保存一级目录；电子学与光学仅在元数据中区分，不增加目录层。历史 data/electronic 与 data/optical 保留原样。

`AcquisitionRequest.consistency` 支持 `full`、`live`、`stream`，默认 `full`；`stream` 需要本次运行的非空 `context_id` 且不能启动物理扫描。批次 `validation=cached` 表示曲线为新读取、配置沿用已有快照，`settings_timestamps` 保留原时间。连续记录缓存帧在 `performance.acquisition_context_id` 中带来源编号，监测缓存不具备记录来源；不能把后者当作完整记录帧。手动显示快照可导出监测缓存及 live 帧，但保留原校验级别、时间及配置来源，不升级为 full。`_session_configuration_version` 和 `monitor_*` 是软件调度元数据，不是仪器读回。历史文件保持原样。

会话快照 `controlsLocked` 只表示本软件连续采集/记录期间的参数锁，纯监测为否；不代表仪器物理面板被锁定。`acquisitionPreparation` 和运行日志记录可选二进制初始化的前后格式、字节序、写入是否尝试及成功/失败状态；不会删除失败后的最后可确认状态。

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
      recording.json
      segment-00001.sqlite3
      segment-00001.csv
      summary.json
    HH-mm-ss-SSS_型号_序列号_single_frame_批次标识/
      recording.json
      snapshot.json
      samples.csv
      samples.npz
  批量采集/YYYY-MM-DD/HH-mm-ss-SSS_batch_批次标识/
    型号_序列号_采集类型_记录标识/
```

日期使用接收操作时的电脑本地日期，同一天共用日期层。每次记录独立创建末级目录，时间精确到毫秒，随机标识防止重名覆盖。型号及序列号清理 Windows 非法字符、保留名及尾部空格/句点；缺失时使用适配器、稳定设备 ID 或明确的未知占位。原始身份仍完整保存在 recording.json。

storage/paths.py 的不可变 `SaveContext` 固定根目录、带时区开始时间、身份、设备 ID、适配器、模式、类型和记录标识。`BatchSaveContext` 预留共用根目录、开始时间及批次路径；仅显式批量任务传入它，分别启动的多台设备仍属于单设备采集。本版不新增批量采集界面。构造上下文不创建目录或曲线文件。

`Repository.export()`、`create_recording()` 和 `Recording` 可选接收 `context`；未传入时仍支持原调用方式并生成新目录。界面保存当前帧、兼容的新读帧导出、连续记录及 CLI 在接受操作时建立上下文，再排队读取/写盘。修改根目录、跨午夜和后台 CSV 转换都不会改变已固定路径；单帧内容保留原帧时间及配置来源。

连续记录开始时立即独占创建目录及 recording.json，版本 1 保存 `kind`、`mode`、`scope`、`run_id`、可选 `batch_id`、`device_id`、`adapter_id`、原始 `identity`、`started_local` 与 `started_utc`。准备失败明确报错并撤销记录状态及参数锁，不切换备用位置。无有效帧即停止仍保留清单和零帧 summary.json，不产生分段曲线文件。

自定义目录只影响采集数据。真实与 Mock 的偏好分别保存在各自库存中；Mock 默认使用 simulation/data，自定义一级目录下则额外使用 simulation 子目录，设置页展示实际生效路径。不迁移库存、槽位、日志、配置、实机档案或历史采集文件。

进程锁固定在每用户 TF-LabControl 根目录，防止换数据目录后启动第二个真实设备进程。SQLite 包含 inventory(id, document)、scans(id, document) 和新增 workspace(id, document)，document 为 UTF-8 JSON。新增表不改变旧库存/槽位格式；会话驻留内存，旧记录不能当成当前已连接。

## 页面工作区

workspace 使用键隔离，真实与模拟分别位于各自根目录：

- `current_mode`：当前模式。
- `data_storage/root`：用户选择的数据保存一级目录的绝对路径；缺省或 null 使用本运行目录的 data。保存前进行创建及写入/读回检查，失败保留原设置。
- `monitor_layout/<mode>`：按顺序保存卡片 `{id, device_id, source, channels, title, view}`。device_id 为稳定库存 ID；不保存列数、仪器句柄或运行状态。
- `draft/<mode>/<device_id>/<channel>/<operation>`：`fields` 保存原输入值及单位倍率，`rows` 保存数组草稿；成功应用后清除该项，失败保留。
- `demo_slots_initialized`：仅模拟入口首次初始化 A/B，避免再次启动覆盖用户绑定。

布局恢复不自动监测；列数根据当前内容宽度重新计算。未知设备或暂时缺少数据源保留卡片并解释状态。

## 库存与资源候选

库存记录包括 id、displayName、identity、identityKey、adapterId、resources、config（主资源）、slots、candidates、discoveryState、driverStatus、lastSeenAt、lastProbeAt、lastError、probeReason 和 scanRunId。

candidates 以原始资源字符串为键，保留 candidateId、scanRunId、source、rawDescriptor、lastSeenAt、config、lastError、probeReason。同一物理设备的多个地址合并后不删除候选；合并前的空身份占位行可被吸收。存在冲突时保留原身份和 identityConflict，连接被禁用，不用新身份覆盖旧资产。

库存及候选新增 presenceState（present/not_seen/unreachable/unknown）；候选的 evidenceScanRunId 标识存在性证据批次，sources 按来源保留 scanRunId、observedAt、rawDescriptor。旧单来源候选在下次合并时兼容补入 sources，无需重建数据库。configured 的 observedAt 只表示配置被枚举，不刷新 lastSeenAt；真实本地枚举或身份成功才更新后者。启动时仅在内存中将旧存在性置为 unknown，并去除旧 evidenceScanRunId，等待当前扫描/会话确认。

稳定 id 是持久化 UUID；可识别身份以厂家/型号/序列号匹配历史记录。序列号缺失时保持候选身份，不能因 COM/IP 相同证明是同一台设备。显示名与槽位保留。连接后才发现历史身份重复时保留 conflict，不在运行 worker 中偷偷更换设备 ID。

`slots` 继续使用按模式索引的映射，例如 `{"electronic": "signal_generator:1", "optical": ""}`。缺少模式键代表该模式尚无绑定记录，空字符串代表明确未绑定。身份合并不得用不同的历史选择覆盖显式空绑定；相互矛盾时保留冲突。同型号不同序列号不会继承槽位。无需新增表或重建旧库存。

`bind_slot(device_id, slot, mode, replace_device_id=...)` 的可选参数表示用户已确认替换的当前占用者；省略时占用仍报错，占用者变化时报 `slot_changed`，须重新确认。两台设备的任务状态和库存占用在同一锁定区间核对，仓储 `save_many()` 用一个 SQLite 事务提交双方记录，成功后才替换内存记录。事务失败不发布半完成状态；`slot_changed` 审计包含被替换设备 ID。

批量连接进度仅在内存保存，包含状态、总数、已处理数、当前设备和逐项结果；结果状态为 `connected`、`already_connected`、`failed` 或 `skipped`，附设备 ID、显示身份和原因。返回的进度与最终结果是独立快照；逐项结果及汇总写入运行目录审计日志。它们不改变库存格式、槽位或实机验收结论。

## 会话快照

snapshots 返回 stableDeviceId、sessionId、resource、adapterId、connectionState、activeTask、pending、lastStatus、lastError、identity。lastStatus 包含状态值和 UTC 时间。

0.3.0 增加 monitoring、recording、configurationVersion、controlPending。监测任务存在时会话持续 busy，批次间也不会误允许断开。recording 在在途记录或写盘尚未结束时继续为真，用于模式锁。控制前值来自已读回/已接受缓存，并写入 operation_requested.before；没有历史值时为 null，不虚构。

发现状态：unknown/identified/stale/unreachable/conflict。identified 要求当前存在证据与身份记录同时成立；仅保存历史 identity 不代表本次已发现。candidate 与 duplicate 是设计概念，本版候选通过 candidates 集合保存，重复资源通过合并或 conflict 表达，不增加虚假的连接状态。连接状态：connecting/connected/busy/degraded/error；没有会话即 disconnected。

扫描失败、取消或部分 provider 失败不覆盖连接快照。只有完整成功覆盖的发现来源才能把未出现的资源标记 stale。configured 地址表示配置存在，不代表网络设备在线。

## 扫描记录

ScanRun 保存 id、startedAt、finishedAt、duration、state、providers 和 items。每个 provider 有数量、成功/错误及错误原因；每项有 device_id、resource、reason、error。开始、结束、provider、探测/缓存/跳过均写审计。

## 测量与质量

Measurement 保存 quantity、unit、values、channel、axis、axis_unit、quality 和 metadata。MeasurementBatch 保存 identity、measurements、configuration、timestamp、time_basis、adapter_version。

ParameterReading 保存 operation、channel、value、source、timestamp、quality、error。MonitorFrame 保存 sequence、configuration_version、request、batch；后台每个数据源保留最近一批，显示层标量趋势限制为 1200 点/600 秒。配置快照新增 `_session_configuration_version`；显式监测记录另含 `monitor_record_session` 和 `monitor_request`。界面抽稀不改变导出数组。

- 标量可以有一个或多个读数；波形和频谱包含对应坐标轴。
- 复数 S 参数在 NPZ 保留 complex dtype；JSON 分 real/imag，CSV 分实部/虚部列。
- snapshot.json 保留身份、设置缓存、传输/前导信息和质量。配置缓存只记录本会话已接受设置及主动读到的元数据，不宣称是设备全量状态快照。
- UTC 时间为主机读回时间；时轴来自仪器前导数据或明确标注的 SDK/TSERVE 周期，不代表跨设备硬件同步。
- quality 为 good/simulated/timeout/disconnected/parse_error/overload/sensor_missing/stale。故障批次使用空数组与错误元数据，不使用零值补齐。非有限 JSON 数字转 null。
- 流式任务先前有效批次保留；中断时审计错误及已提交路径，不生成冒充有效帧的空数据文件。

NPZ 使用 NumPy 数据数组，不保存 pickle 对象。CSV 使用 UTF-8 BOM 便于中文 Excel 打开。Touchstone 要求 S11 或完整同一频率轴 S11/S21/S12/S22，顺序按规范，参考阻抗显式传入；不虚构缺失 S 参数。

### 连续记录格式 v1

连续记录目录含 segment-00001.sqlite3 等分段文件、对应 CSV 与 CSV 检查点，以及停止时的 summary.json。SQLite user_version=1：configs 保存配置和原始设置时间，按 SHA256 内容版本去重；frames 保存递增序号、配置版本引用、时间、身份、质量验证级别和性能；samples 保存各通道元数据、dtype、shape 及 values/axis BLOB，保留复数、字节序和精度，不使用 pickle。一个帧的所有通道和配置引用在同一事务提交，转换进程只读取已提交帧。

CSV 增加 sequence、config_id 列；检查点保存完整帧对应的序号、文件偏移和帧数，重启时截断未提交尾部后续写，避免重复行。连续 CSV 为 UTF-8，旧单帧 CSV 仍使用 BOM。可离线执行 `python -m lab_control.storage.recording --csv <记录目录>` 恢复转换，不连接仪器。read_frames 支持分段格式读取；旧 JSON/CSV/NPZ 导出不改变。

录制摘要及会话状态区分 received、pending、committed、converted、paused、error、csv_error；已收到不等于已落盘，CSV 转换失败不修改二进制提交数。start_acquisition 仍返回路径列表，路径单位改为分段文件，实际帧数见摘要。MonitorFrame 增加单调时钟 received_at 用于显示延迟，不用它替代 UTC 数据时间。

## 审计和报告

审计记录连接参数、目标身份、操作请求/结果、步骤数、失败与清理事件；流任务记录开始/停止和错误。报告含测试范围、目标、时间、请求/读回、数据路径及 hardware_accepted=false。报告成功只代表执行了该次检查，不自动提升设备验证等级。

HDF5、高频无限流写盘、完整实验/阈值索引、遥测和科学数据解析仍为后续设计。附件载荷帧格式为空，本轮没有猜测数据包定义。
