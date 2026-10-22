# 设备兼容层（0.3.0 应用接口）

公共核心不依赖 Qt。代码以领域模型、传输、型号能力、应用服务和持久化分层。真实能力以[自动生成矩阵](../generated/capability-matrix.md)为准，不以旧文档中拟定的类名或接口清单判断已支持。

## 公共对象

定义位于 src/lab_control/domain/instruments.py。

| 对象 | 用途 |
| --- | --- |
| DeviceIdentity | 厂家、型号、序列号、固件、可信度、原始身份 |
| Parameter / Capability / CapabilitySet | 类型、单位、范围、枚举、通道作用域、选件、确认要求和资料来源 |
| DeviceCommand | 操作名、受校验参数、通道、显式 confirmed |
| ApplyResult | success / rejected / partial / uncertain，步骤数、读回、错误 |
| AcquisitionRequest | 数据类型、通道、点数、quantity、是否发起采集及确认，可选时长与采样率 |
| Measurement / MeasurementBatch | 标量、波形、频谱、复数轨迹、坐标轴、质量、配置、时间戳 |
| DeviceStatus / DeviceError | 状态读回和带错误码的异常 |
| ParameterReading | 操作、通道、值、来源、UTC 时间、质量和错误 |

InstrumentAdapter 提供 identify/capabilities/read_status/read_settings/apply/acquire/close。SDK 与 SCPI 使用相同上层接口；SDK 不伪装字节 transport。

```python
def read_settings(self, operations: tuple[str, ...], channel: int = 1) -> tuple[ParameterReading, ...]: ...
```

来源包括 instrument（已核实查询/API）、service_readback（服务配置实际查询）、session_cache（本会话设置记录）、control_cache（已确认控制结果）、simulated 与 unavailable；服务配置读回不能作为物理频率或性能测量。未知值为 None/quality=stale，不补零；自动模式的结构化读回可保留可选数值字段为空，不将其误判为整个设置未知。SCPI 复用 Profile 查询；SDK 没有核实 getter 时仅回报缓存，禁止猜测 API。UI 同时展示来源和 UTC 时间，且不会用新读回覆盖用户草稿。时间表示本次读取/缓存报告时间，不代表设备硬件时钟。

## 注册与型号

instruments/registry.py 提供 match、describe、create_adapter。describe 只读能力元数据，不打开设备、不加载 DLL。注册统一来自 devices/catalog.py；已迁移型号的 Profile、协议、编码器、范围及文档留在独立包，公共层仅复用通用会话与传输。非 SCPI 型号通过适配器工厂、能力描述和只读发现入口接入，扫描不启动服务。扩展约束见[模块维护规范](../architecture/device-modules.md)。

AFG31252 使用 AFG31000 手册；ZNB3004 使用 ZNB3000 手册。DS1202Z 不自动变成 DS1202Z-E；PSW160 不推定厂家/后缀。Microchip 53100A 使用独立 TSERVE 模块，本机连接时实际读回 USB 身份描述符，来源独立标记；系统枚举缓存、铭牌输入和可修改服务标题不替代实际读回，服务配置与测量验收分开。未知型号只保留库存元数据，UnknownDeviceAdapter 拒绝控制和采集。

模拟设备使用独立 SimulatedInstrument；待定温控、负载、泵浦、光谱和光功率类别提供模拟参数，不注册为真实设备。

## 类别能力与高级功能

信号源覆盖基础波形和电参数、AM/FM/PM、扫描/突发、触发和部分任意波形上传；射频源使用频率/功率与列表。电源分别处理通道输出、整机输出、电压电流、保护、列表、远端采样和跟踪；没有独立通道输出的型号不虚构该能力。

示波器覆盖通道、时基、触发、运行/单次/停止、量程、波形导出及部分平均/高分辨率设置；Pico 提供 block 和有限 streaming 批次。频谱、光功率、万用表、网络分析和相噪类别分别使用原生测量模型。能力范围和高级功能缺项逐型号列在矩阵中，未实现操作返回 unsupported。

能力表里的参数范围是保守软件边界，不代表设备在所有频率/负载/选件下都达到该范围。通道限制和用户 limits 取较严格值；不能通过配置扩大上限。设置信号源幅度前检查 VPP 单位及幅度/偏置包络；频率扫描两端点检查顺序；SDK 波长检查当前探头动态范围。设备仍可能量化、拒绝或受关联参数约束。

## 调用与控制结果

    future = app.sessions.submit(
        device_id, "apply",
        DeviceCommand("frequency", {"value": 1000}, channel=1),
    )
    result = future.result()

每设备一个 worker，完整操作（选通道、设置、读回、错误查询）串行执行，不能把它拆成多个 UI 请求。UI/CLI 不提供任意厂家命令入口。输出、触发、波形上传、归零等能力需要 confirmed；CLI 写入还需 --write。

success 表示本次传输/设备响应通过相应检查；无读回能力只说明调用被接受。rejected 包括设备错误和读回不一致，设备可能已经部分改变；partial/uncertain 要停止后续任务并人工核对。不会回滚或自动重放，也不会用复位作为恢复手段。设置已开启的输出也可能改变物理信号。

## 采集与任务

SessionManager.submit 返回 Future。start_acquisition 在同一 worker 上重复采集并导出批次，stop_acquisition 设置停止事件，当前 I/O 返回后停止。subscribe 接收连接、任务、结果、测量和错误事件。

有限任务由适配器声明 `recorded_run_kinds`，提供 `validate_recorded_run(request)` 和 `run_acquisition(request, stop)`；公共会话复用同一 worker、有界逐帧保存与收尾。`AcquisitionRequest.duration` 和 `sample_rate` 分别为秒与 samples/s，型号负责范围和结束条件，不能另传 count 或抽帧保存间隔。自然结束、主动停止和异常均释放任务；硬件停止失败报告未知状态，仍排空已提交数据，后台 CSV 不延长参数锁。未支持有限任务的适配器拒绝这些字段，旧请求保持原行为。

GUI 的纯监测使用独立 MonitoringService，避免沿用 CLI start_acquisition 的默认落盘语义：

```python
app.monitoring.start(
    device_id, (AcquisitionRequest("waveform", (1, 2), 2000),), interval=0.5, mode="general"
)
app.monitoring.apply(device_id, DeviceCommand("timebase", {"value": 0.001})).result()
frame = app.monitoring.latest(device_id, "waveform")
app.monitoring.set_recording(device_id, True, mode="general")
app.monitoring.stop(device_id).result()  # 不关闭物理输出
```

该服务只向原设备 worker 提交整项操作。相同 kind/quantity 合并通道，保存最新 MonitorFrame；参数应用后 configuration_version 增加，旧帧不能重新覆盖新配置画面。rejected/partial/uncertain/错误事件只暂停目标设备；设备原会话继续保留供诊断。CLI 的旧接口与写入/确认要求不变。

纯监测保存有界缓存和审计，显式记录才保存批次。当前统一使用 general 工作区，旧模式值仅保留接口及历史元数据兼容。停止记录时，已授权的在途批次仍完成保存；二进制收尾后释放参数锁，CSV 转换继续后台执行。格式与路径见[数据模型](../data/data-model.md)，任务规则见[实时监测设计](../requirements/realtime-monitoring.md)。

台式仪器默认读取当前数据；明确 initiate 才发起单次采集，且需要确认。多通道顺序读取，应使用单次/停止状态保证帧一致；本版不提供跨仪器硬件同步。Pico 必须先显式配置通道；streaming 是有限批次，批次间有空隙。3458A RMEM 会停内部存储，读取也要求确认。TSERVE 数据口不发送命令，只读取配置好的 P/F 格式。

时间戳为主机 UTC 读回时间，坐标轴采用设备元数据；不能当作硬件同步时间。故障批次保留质量标记和错误，原始有效批次继续保留。

## 会话与发现

SessionManager 独占设备/别名租约。扫描、连接共享原子检查；扫描不改变连接状态、不插入身份查询。缓存身份、发现状态、连接状态、当前任务分别记录，详见[唯一扫描设计入口](../requirements/device-discovery-and-overview.md)。

## 扩展规范

1. 先取得准确型号与厂家命令/API 依据；补型号卡片和版本。
2. 复用 Parameter/Capability/Measurement，注册真实支持的操作，注明选件及作用域。
3. 将型号协议和差异写入独立型号包，仅通过设备目录注册；SDK 延迟加载。
4. 增加按型号归档的协议夹具、异常路径和有效采集测试；测试不能打开默认硬件。
5. 运行 Ruff、mypy、pytest，重新生成能力矩阵与卡片；实机报告另行验收。

未完成的复杂序列、完整分段采集、无间隙无限流和部分选件应用明确列为后续扩展，不通过声明空方法充数。
