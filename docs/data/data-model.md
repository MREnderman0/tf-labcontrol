# 数据模型与运行文件（0.3.0）

领域对象定义在 domain/instruments.py，持久化由 storage/repository.py 完成。电子学与光学共享结构，但分别导出到 data/electronic 与 data/optical。

## 实际存储结构

    %LOCALAPPDATA%/TF-LabControl/       # LAB_CONTROL_HOME 可覆盖
      config/                         # 用户自行保存的 UTF-8 TOML
      inventory.sqlite3               # WAL 模式
      logs/YYYY-MM-DD.jsonl           # UTF-8 审计
      data/electronic/<UTC-UUID>/
        snapshot.json
        samples.csv
        samples.npz
      data/optical/<UTC-UUID>/
      reports/<UUID>.json
      simulation/                     # --mock 独立存储

进程锁固定在每用户 TF-LabControl 根目录，防止换数据目录后启动第二个真实设备进程。SQLite 包含 inventory(id, document)、scans(id, document) 和新增 workspace(id, document)，document 为 UTF-8 JSON。新增表不改变旧库存/槽位格式；会话驻留内存，旧记录不能当成当前已连接。

## 页面工作区

workspace 使用键隔离，真实与模拟分别位于各自根目录：

- `current_mode`：当前模式。
- `monitor_layout/<mode>`：按顺序保存卡片 `{id, device_id, source, channels, title, view}`。device_id 为稳定库存 ID；不保存列数、仪器句柄或运行状态。
- `draft/<mode>/<device_id>/<channel>/<operation>`：`fields` 保存原输入值及单位倍率，`rows` 保存数组草稿；成功应用后清除该项，失败保留。
- `demo_slots_initialized`：仅模拟入口首次初始化 A/B，避免再次启动覆盖用户绑定。

布局恢复不自动监测；列数根据当前内容宽度重新计算。未知设备或暂时缺少数据源保留卡片并解释状态。

## 库存与资源候选

库存记录包括 id、displayName、identity、identityKey、adapterId、resources、config（主资源）、slots、candidates、discoveryState、driverStatus、lastSeenAt、lastProbeAt、lastError、probeReason 和 scanRunId。

candidates 以原始资源字符串为键，保留 candidateId、scanRunId、source、rawDescriptor、lastSeenAt、config、lastError、probeReason。同一物理设备的多个地址合并后不删除候选；合并前的空身份占位行可被吸收。存在冲突时保留原身份和 identityConflict，连接被禁用，不用新身份覆盖旧资产。

稳定 id 是持久化 UUID；可识别身份以厂家/型号/序列号匹配历史记录。序列号缺失时保持候选身份，不能因 COM/IP 相同证明是同一台设备。显示名与槽位保留。连接后才发现历史身份重复时保留 conflict，不在运行 worker 中偷偷更换设备 ID。

## 会话快照

snapshots 返回 stableDeviceId、sessionId、resource、adapterId、connectionState、activeTask、pending、lastStatus、lastError、identity。lastStatus 包含状态值和 UTC 时间。

0.3.0 增加 monitoring、recording、configurationVersion、controlPending。监测任务存在时会话持续 busy，批次间也不会误允许断开。recording 在在途记录或写盘尚未结束时继续为真，用于模式锁。控制前值来自已读回/已接受缓存，并写入 operation_requested.before；没有历史值时为 null，不虚构。

发现状态：unknown/identified/stale/conflict。candidate 与 duplicate 是设计概念，本版候选通过 candidates 集合保存，重复资源通过合并或 conflict 表达，不增加虚假的连接状态。连接状态：connecting/connected/busy/degraded/error；没有会话即 disconnected。

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
- 流式任务先前有效批次保留；中断时另存故障批次及已完成路径。

NPZ 使用 NumPy 数据数组，不保存 pickle 对象。CSV 使用 UTF-8 BOM 便于中文 Excel 打开。Touchstone 要求 S11 或完整同一频率轴 S11/S21/S12/S22，顺序按规范，参考阻抗显式传入；不虚构缺失 S 参数。

## 审计和报告

审计记录连接参数、目标身份、操作请求/结果、步骤数、失败与清理事件；流任务记录开始/停止和错误。报告含测试范围、目标、时间、请求/读回、数据路径及 hardware_accepted=false。报告成功只代表执行了该次检查，不自动提升设备验证等级。

HDF5、高频无限流写盘、完整实验/阈值索引、遥测和科学数据解析仍为后续设计。附件载荷帧格式为空，本轮没有猜测数据包定义。
