# Microchip 53100A

注册标识 `microchip.53100a`；逻辑测量通道 1。本包是型号协议、范围、测试和文档的唯一维护源。

## 能力与协议

USB 使用厂家 TimeLab 驱动，项目通过 TSERVE64 访问；不发送 VISA/SCPI 命令。支持身份与状态、输入/参考标称频率和相位采样率读写、定时相位采集、相噪、重叠 ADEV 与 MDEV。旧接口仍支持显式开始/停止、AM 噪声、服务 ADEV 和外部 P/F 数据流。

依据：[Microchip DS50002991G](https://ww1.microchip.com/downloads/aemDocuments/documents/FTD/ProductDocuments/UserGuides/53100A-Phase-Noise-Analyzer-Users-Guide-DS50002991.pdf) §1.5、§8；[TimeLab 正式版说明](https://www.miles.io/timelab/readme.htm) 1.730 的 `show serial`、1.780A 的空闲状态修复。`show message` 会清除消息，因此常规状态与只读入口不使用该查询。

外部服务的 `show serial` 必须返回真实序列号并匹配预期目标。本机托管服务允许独立读回所选 USB 的标准身份描述符，来源标为 `usb_descriptor`；不以配置值、系统枚举缓存或可修改的服务标题证明身份。服务版本独立保存；未能解析硬件固件时保持未知。

型号元数据将 `tserve` 声明为扫描只保留候选、显式连接后确认身份的传输；扫描不为身份探测启动服务。此策略不扩大到未列出的传输，也不改变其既有支持边界。

## 连接与驱动

Windows 10/11 x64；组件 ID `tserve`，详见[组件台账](../../../../../deployment/components.json)及[材料与安装依据](deployment.md)。材料为 Miles Design 官方 TimeLab 1.780A 正式版，下载缓存及安装包签名/哈希已登记；安装结果、驱动签名及设备条件以运行目录为准。分发许可和干净目标电脑验收仍须核实，不将开发机结果视为发布通过。

扫描用系统 PnP 枚举 `VID_16D0&PID_05B8`，缺驱动仍显示候选；不打开设备、不启动服务。首次连接填写核实的仪器序列号，连接配置保存 `usb_instance_id`；`server_path` 可覆盖标准目录的 TSERVE64 绝对路径。程序取得设备锁后启动独占子进程，以 `/serial` 绑定目标并校验监听 PID，不添加 `/start`。只清理本次创建的服务，外部程序占用不强制解除。

旧 `transport="tserve"`、`resource="tcp://host:1299"` 配置继续连接已有服务，要求 `identity_confirmed=true` 确认控制权；断开外部服务不会发送 shutdown。托管模式分配两个不同的临时 TCP 端口，分别核对命令和数据监听 PID，客户端仅连接 127.0.0.1；不修改网卡或防火墙。安装需要的管理员操作独立进行，不自动重启或降级共享驱动。

## 定时采集与结果

正式入口“实时监测 → 相噪仪”（设备管理同类别也跳转此页）提供采样率、时长、输入/参考自动或手动频率、开始/停止、进度和数据目录。开始按钮授权一次测量，切页或连接不会启动；其他高级项目暂不加入页面。界面上方为相噪，下方共轴显示 ADEV/MDEV，均保留结束结果。

采样率可选 **1、10、100、1000 samples/s**，开始前 `set phaserate` 后以 `show phaserate` 实际核对；频率设置先应用并读回。时长 **10～86400 s**，且 `采样率 × 时长 + 1 ≤ 4,000,000`。时长按首末有效样本时间差定义，不含初始化；初始化最多等待首点 30 秒。采集时不能修改设置。正常测量设置保留用户所选值，不自动恢复为测试前配置。

托管启动固定 `/format:P /timestamp:s /phasedec:2 /sep:32 /ch:3-1 /notify /smooth:0`，不加 `/start`。相位单位秒、单路 J3−J1、时间轴为 TSERVE 相位样本计数对应秒数，既不是主机收包时间，也不是 UTC 硬件时标。默认 decimation=2 时有效采样率即所选 phaserate。采样率控制低频相位流及稳定度最小 τ，不限制仪器相噪频谱的偏移频率上限。`show phasediff` 只有每秒一点，因此不用于这条采集链路。

每次使用一个持久数据连接，逐段检查时间戳间隔；缺点、重复、通知告警、非有限值和格式错误丢弃当前段，不补零或插值。连续采集交公共层重连，保留原自有 TSERVE 服务，重新核验身份及采样率/频率设置；仍为 Collecting 时附接新数据连接，不重放 start 或设置。Ready 时可按剩余有效样本预算开始新段；不确定的 start 应答停止并报告，不能自动重放。首次有效段再次用活动采集 `show serial` 核对硬件。逐段核对状态和配置；每五个采样秒及最终段读取 `show spectrum`，接受官方表头，空结果标为未就绪，不制造曲线。频谱未平滑、不移除杂散。相噪频谱是仪器截至查询时的累计结果，其时间窗口不等同于主机的相位样本裁剪窗口。

恢复后的相位段使用新 `acquisition_epoch`，保存 `has_data_gaps` 与 `stability_scope=current_contiguous_epoch`；ADEV/MDEV 只计算本段连续样本，不跨断线拼接。原批次已提交片段继续保留，重连不重新分配全部时长。离线时停止若无法重新确认通信，明确报告物理测量停止未确认、最终状态未知。当前自动恢复已做协议和公共流水线模拟回归，带信号实机恢复仍待验证；服务进程退出后的重启不属于当前仅重开连接的路径。

采满 N 点后停止 TSERVE 并核对 Ready，再输出计算结果。提前停止保留实际取得的连续片段，明确标为部分时段。停止不确认时报告最终仪器状态未知，不发送第二次 stop/start。通信超时不重放设置；停止失败与原错误都保留异常链。

`stability.py` 独立实现 [NIST SP 1065](https://tf.nist.gov/general/pdf/2220.pdf) 的相位数据重叠 Allan/modified Allan 方差公式，参考 [AllanTools 统计量说明](https://allantools.readthedocs.io/en/stable/functions.html)的接口解释，不引入或复制其代码。τ 使用 1、2、4、8…个采样间隔，每点至少两个有效求和项，保存项数；不去趋势、不估计置信区间。数据不足时不生成该曲线；零偏差保留在文件，因对数坐标不绘制零点。

原始相位分段、相噪、最终 ADEV/MDEV 和配置进入公共有界保存流水线，后台生成 CSV；“打开数据目录”进入本批分析目录。失败保留已提交片段，未达时长不标完成。外部 TSERVE 未核验时间戳和流格式，因此新定时能力仅在本机托管 USB 连接开放，旧外部 API 保留。

每批配置保存 `sample_rate`、`requested_duration`、实际 `samples`、`elapsed` 和 `first_timestamp`；相位坐标保留服务原时间戳，`elapsed` 为实际连续样本覆盖的时长。正常收尾结果为 `final=true`、`stopped=true`、`stop_state="Ready"`，只有达到目标点数才有 `complete=true`；提前停止为 `complete=false`，停止失败不产生成功收尾结果。相噪仅在实际查询时加入过程帧，元数据 `queried_at` 与 `phase_samples_at_query` 保存该次查询的主机 UTC 时间和相位点数；最终结果携带最近频谱时保留原查询时间，不冒充最终时刻重新测得。

## 单位与限制

输入/参考频率单位 Hz，范围 **1 MHz～200 MHz**，命令单位 MHz；`automatic=true` 恢复厂家 `auto` 模式。未给 `automatic` 时按手动值处理，必须提供 `value`。仅 Ready 状态允许频率修改，每次写入后 `show inputfreq/referencefreq` 实际读回；读回来源为服务配置，不是物理频率测量。

开始/停止必须显式确认。传输超时关闭连接、不重发写入；恢复前重新核对实际状态。空闲、缺信号、非有限值、轴异常或不完整帧均不得产生有效测量批次。采集使用厂家确认的 ASCII TSERVE 接口；没有已验证的二进制替代格式，不猜测二进制协议。外部数据流仅支持已明确配置的 P/F 单列、无时间戳和一致采样周期。

TimeLab 1.780A 的服务报告版本为 1.78，两者分别登记。空闲 `show serial` 返回无活动连接；Ready 只检查 VID/PID 存在。已验证的本机解法是在显式连接并持有资产锁时，通过 Windows 标准 GET_DESCRIPTOR 读取选定端口的厂家、型号和序列号。先比对端口驱动键及 VID/PID，再按官方随包 `timelab.cpp` 的 `USBDEVICE::serial` 规则去除序列号冒号或空格后的附加字段，与铭牌预期值比较。连接前和身份确认时分别读回；扫描仍只查 PnP。

该路径允许核实 USB 设备并连接空闲的自有 TSERVE，记录 `hardware_acquisition_open=false`，不声称 TSERVE 已打开硬件采集。外部服务、描述符读取失败或目标不匹配不采用此后备方式。初期只有服务配置验证的历史批次保留原结论；最新只读批次已通过 USB 身份、兼容层连接及重连。原始 USB 请求/回包和厂商源码哈希留在运行目录。实际测量仍需另行明确接线与启动授权。

## 文件与公共依赖

`adapter.py`、`profile.py`、`transport.py` 维护协议；`discovery.py`、`server.py` 维护设备发现与服务生命周期；`tests/fixtures` 保留迁移前与当前契约。公共层只提供会话、设备锁、PnP 元数据、存储及非 SCPI 工厂钩子。

## 测试入口与实机状态

软件：`python -m pytest src/lab_control/instruments/devices/microchip_53100a/tests`。实机入口和授权范围见[测试计划](hardware/README.md)。原始身份、地址、通信和参数状态只保存运行目录。

| 功能 | 接口及条件 | 验证范围 / 状态 | 证据位置 |
| --- | --- | --- | --- |
| 枚举与驱动 | USB / 仅电源 | 2026-09-28：从代码 28 缺驱动恢复到代码 0；驱动 3.4.7.1 | hardware-tests 的安装与绑定批次 |
| USB 身份、兼容层连接及重连 | USB + 本机 TSERVE | 2026-09-28 新只读批次通过；身份来自实际 USB 描述符，采集未打开 | 原始 results.json、summary.json 与 USB 应答 |
| TSERVE 直接硬件身份、固件 | TSERVE Ready | 服务不能在空闲返回硬件序列号，固件未知 | 单列 tserve_hardware_identity 受阻 |
| 服务连接、重连和退出 | TSERVE 1.78 / Ready | 新只读批次和新控制批次通过，正常退出自有服务 | 通信与服务进程日志 |
| 标称频率配置 | TSERVE Ready | 新控制批次通过：两项分别设置 10 MHz、实际读回、确认另一项不变、恢复 auto；仅服务配置验证 | 通信与恢复快照 |
| 采样率、定时采集、相噪、ADEV/MDEV | 缺 DUT / 参考信号 | 软件协议、数值算法、公共收尾与界面回归；带信号实机 not_tested | [软件报告](../../../../../docs/reviews/phase-workspace-review.md) |
| AM、旧外部数据流 | 缺 DUT / 参考信号 | 保留接口；本轮实机 not_tested | 资产功能覆盖表 |

## 已知限制与待测

| 功能或问题 | 限制 / 原因 | 后续验证 |
| --- | --- | --- |
| 完整测量与性能 | 本轮只有 USB 和电源 | 明确 DUT/参考频率、电平与接法后独立补测 |
| 托管定时测量 | 连续时间戳相位流已实现，物理信号尚未测试 | 四档采样率、连续性、定时停止、与 TimeLab/NIST 数据交叉核验 |
| 首次身份 | 本机通过标准 USB 描述符实际读回；TSERVE 直接身份在空闲不可用 | 采集时再比对 show serial、固件和输入状态；远程服务不能借本机 USB 身份通过 |
| 提示符边界 | TSERVE 提示符的末尾空格必须一起读取，设置命令可能返回空正文 | 已补空应答和分帧回归；原失败批次保留，恢复结论按批次查询 |
| 范围契约 | 修正旧实现 1 Hz 下限为厂家 1 MHz；增加 auto 和查询 | 旧非法范围现在明确拒绝；迁移基线保留 |

## 扩展与移除

只修改本包和唯一注册目录，通过生成器刷新派生说明；撤销注册后旧配置应提示型号不可用，保留用户配置和全部实机历史。
