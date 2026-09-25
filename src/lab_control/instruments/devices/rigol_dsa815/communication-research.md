# DSA815 上位机与通信实现调研

核对日期：2026-09-25。此文记录官方资料和实际源码对照，以及后续优化的验证顺序。没有运行第三方控制程序、连接仪器或切换本项目传输方式；候选方案尚未形成新的性能承诺。当前行为仍以[型号说明](README.md)为准，共性要求见[采集规则](../../../../../docs/architecture/live-acquisition.md)。

实施更新：随后按用户确认方案实现公共计时、流读取、完整保存调度和 DSA815 轻量路径，并提供独立性能验收入口。以下“本次核对”“待验证”描述保留研究时点的证据边界；现行接口见型号说明，实机结论只以正式运行目录的验收批次为准。

验收补充：普通曲线的 Socket 读取成功不能证明全部命令可迁移。厂家 2-179 页的 `SYST:OPT?` 返回多行表格，VXI-11 的消息结束标志与 Socket 换行分帧不同。只取表头会让后续行串入下一查询；目前发送前限制 Socket 选件查询，保留原传输。候选的完整备份/恢复也必须预检，不能凭采集测速结果启用偏好。相关失败及恢复状态留在正式运行目录。

后续落地：目标固件的 VISA VXI-11 标量复合查询已通过独立只读对照、60 秒监测和三次重连，按型号说明限定组合启用，full/live 各保留原字段并降为三次事务。单独切换文本换行终止符没有改善，未采用。公共层增加 read-up-to 预取、分片增量扫描、原生 TCP/VICP 的 TCP_NODELAY，以及低频/错峰后台同步；监测和采数期间暂停独立后台参数读取。DSA815 普通元数据按 30 秒刷新、关键字段逐帧核对、保存完整核对。性能证据保存在运行目录，下面的研究阶段推断不作为当前未完成清单。

## 官方软件确实存在

[RIGOL 官方软件下载页](https://www.rigolna.com/support/software/)明确列出：

| 软件/资料 | 用途与本次可确认的边界 |
| --- | --- |
| UltraSpectrum | DSA800 等系列的仪器控制与频谱分析软件；官方下载页提供 15 天试用。没有获得其内部源码，不能把推测的轮询方式或刷新速度当成厂家实现。 |
| UltraSigma | 基于 VISA 的基础通信工具，也是 UltraSpectrum 的安装/启动环境。适合后续作为独立通信对照，但本次未安装或运行。 |
| S1210 | EMI 预兼容测试软件；用途不同于普通实时曲线监测。 |
| LabVIEW 2010 Example Basic DSA Control | 厂家列出的 DSA800 等系列曲线读取示例，要求 LabVIEW 与 VISA；本次未检查 VI 内部实现。 |

[NI 的 DSA800 驱动页面](https://sine.ni.com/apps/utf8/niid_web_display.download_page?p_id_guid=CA1FA4FE2A1C6B71E0440021287E65E6)还列出经认证、测试机型为 DSA815 的 LabVIEW Plug and Play 驱动。它是另一个对照来源，不等同于已研究过源码的开源上位机。

## 已阅读的开源实现

### Lab::Measurement：直接支持 DSA815 的仪器库

审阅版本 `7c949ff27ccbc56b350970698cf6f43dca2713ca`。

- [Rigol_DSA815.pm](https://github.com/lab-measurement/Lab-Measurement/blob/7c949ff27ccbc56b350970698cf6f43dca2713ca/lib/Lab/Moose/Instrument/Rigol_DSA815.pm)将点数固定为 601，复用频谱采集、格式和二进制块处理角色。
- [SpectrumAnalyzer.pm](https://github.com/lab-measurement/Lab-Measurement/blob/7c949ff27ccbc56b350970698cf6f43dca2713ca/lib/Lab/Moose/Instrument/SpectrumAnalyzer.pm)中，`get_traceY`通过一次轨迹查询取数据；格式准备另行处理。`get_StartX`/`get_StopX`使用缓存的频率端点，`get_loggable_state`独立读取带宽、单位、电平等状态。
- [SCPIBlock.pm](https://github.com/lab-measurement/Lab-Measurement/blob/7c949ff27ccbc56b350970698cf6f43dca2713ca/lib/Lab/Moose/Instrument/SCPIBlock.pm)选择二进制精度并解析块；[Format.pm](https://github.com/lab-measurement/Lab-Measurement/blob/7c949ff27ccbc56b350970698cf6f43dca2713ca/lib/Lab/Moose/Instrument/SCPI/Format.pm)与[Cache.pm](https://github.com/lab-measurement/Lab-Measurement/blob/7c949ff27ccbc56b350970698cf6f43dca2713ca/lib/Lab/Moose/Instrument/Cache.pm)提供格式及设置缓存。

值得借鉴的是曲线读取和完整状态记录分离。不能直接照搬缓存策略：这些函数本身不能证明它会及时察觉前面板修改。本项目还需保护零跨度坐标、单位及 Auto 后配置变化。该库的连接初始化包含清除操作，格式准备可能写入仪器；研究中没有运行这些代码，也不采用其初始化副作用。

### emc-toolkit：小型 DSA800/DSA815 工具

审阅版本 `802fe6c626a0127d747b986b9dd03993bffd8d9c`，文件 [dsa800.py](https://github.com/fpgadeveloper/emc-toolkit/blob/802fe6c626a0127d747b986b9dd03993bffd8d9c/Python/emctools/src/rigol/dsa800.py)。

它保留 PyVISA 会话，`get_trace`只查询一次 `:TRACe:DATA? TRACE1`，设置读写在其他方法中。这说明普通轨迹传输不必每次附带一套完整设置轮询；不证明只读一次即可满足本项目所有坐标与状态一致性要求。

进一步核对发现，其 `connect` 显式设置 `query_delay=1`：每次高层查询在发送和读取之间等待一秒。这是示例自身的选择，不能归因于 PyVISA 默认开销。仅依据轨迹传输耗时估算的帧率不代表该示例原样运行的速度。本项目直接串行发送/读取，没有这一固定等待。`Lab::Measurement` 的默认单精度二进制与设置缓存也不是相同的查询工作量，比较必须控制格式、校验级别与查询序列。

此项目仅作为简洁路径对照。它的 ASCII 解析假定特定块头，不能替代本项目的完整回包、点数、有限值检查；系列命令也不能全部视为 DSA815 支持。没有直接引入第三方代码。

### ngscopeclient / scopehal：类似仪器上位机的架构参考

审阅版本 `e812615f5d1ce9498952fd1f6847f8bda151d5a3`。本次研究的是 Rigol 示波器驱动与公共 SCPI 传输，不声称该驱动支持 DSA815。

- [RigolOscilloscope.cpp](https://github.com/ngscopeclient/scopehal/blob/e812615f5d1ce9498952fd1f6847f8bda151d5a3/scopehal/RigolOscilloscope.cpp)有设置缓存和 `FlushConfigCache`。部分机型采集通过 `WAV:PRE?`获得成组波形描述，再读取数据；这些示波器命令不适用于 DSA815。
- [SCPITransport.cpp](https://github.com/ngscopeclient/scopehal/blob/e812615f5d1ce9498952fd1f6847f8bda151d5a3/scopehal/SCPITransport.cpp)通过同一互斥锁保护查询发送与回复读取，并提供排队发送。队列不等于自动把全部命令合并成一条 SCPI 查询。
- [SCPISocketTransport.cpp](https://github.com/ngscopeclient/scopehal/blob/e812615f5d1ce9498952fd1f6847f8bda151d5a3/scopehal/SCPISocketTransport.cpp)设置 TCP 延迟选项并声明可批量发送；[SCPILxiTransport.cpp](https://github.com/ngscopeclient/scopehal/blob/e812615f5d1ce9498952fd1f6847f8bda151d5a3/scopehal/SCPILxiTransport.cpp)的 VXI-11 实现不支持同类 batching。参见其[传输说明](https://www.ngscopeclient.org/manual/Transports.html)。这是该项目的传输能力边界，不是所有 VISA 实现的性能结论。
- 其[调试控制台说明](https://www.ngscopeclient.org/manual/MainWindow.html)提醒直接发命令会绕过驱动缓存。因此缓存必须有失效路径，不能仅靠界面写入时更新。

本项目已有会话 worker 和串行采集，应在既有结构上改进；不能为加速创建第二个并行连接来读同一仪器。

## 官方示例的进一步核对

DSA800 编程手册第 3 章的 C++/VISA 示例使用 `viRead` 和实际 `retCount`，3-9 至 3-10 页的异常处理包含固定等待、重复读写和重连。本项目借鉴按实际返回长度处理数据，保留现有的超时停止规则，不采用示例中的自动重发。厂家列出的 LabVIEW/VBA DSA 下载本次未成功获取，不能声称已经检查 VI 或宏的内部实现。

[R&S 官方速度优化说明](https://www.rohde-schwarz.com/us/driver-pages/remote-control/speed-optimization_231252.html)建议先用通信计时定位开销、保持测量同步，并在目标支持时合并 SCPI 命令。其示例主要是设置命令，不足以证明 DSA815 复合查询的回复数量、分隔符和结束边界。DSA800 用户手册的命令/查询错误章节说明了消息分隔与未完成查询的约束；任何复合查询仍需对选定型号做独立只读验证。仪器显示关闭、平均或 Max Hold 会改变用户的测量行为，本项目不为了提速自动改变这些设置。

[R&S 官方 RsInstrument 源码](https://github.com/Rohde-Schwarz/RsInstrument/blob/b600fd6cf693dbcfa3cbfba0f638dfa14ff2c0aa/RsInstrument/Internal/VisaSession.py)区分已知长度二进制与未知长度回复；无消息结束标志的连接不能据此读取未知长度二进制。核对提交 `b600fd6cf693dbcfa3cbfba0f638dfa14ff2c0aa`，文件 SHA256 为 `8f33e48a20b0660fa9b93f1d1ed77d17342c8fe6048d9865ce30654280ec9ffc`。其块头逐字节读取方式也不是所有传输的最快选择。公共流读取现对明确支持“读取至多若干字节”的后端预取头部和正文，保留分包、粘包和二进制内换行；只提供精确长度读取的后端仍使用保守路径。

scopehal 的 Socket 初始化关闭 Nagle。本项目为原生 TCP/VICP 设置 `TCP_NODELAY`，不修改系统网络设置；VISA 后端选项没有根据其他实现的默认值盲目覆盖。该选项本身不构成实机性能改善证据。长文本分片读取从上次扫描的重叠位置继续寻找终止符，避免随回复增长反复扫描整个缓冲区。

## 厂家协议与传输候选

依据已核对的[英文 DSA800 Programming Guide](https://beyondmeasure.rigoltech.com/acton/attachment/1579/f-06f0/1/-/-/-/-/dsa800programpdf.pdf)，文件哈希见[型号说明](README.md)：

- 3-2 页明确给出 Socket 编程端口 **5555**。所以原始 TCP 是有厂家依据的候选，不是从示波器猜出的端口。该下载链接本轮浏览工具获取失败，页码依据运行目录此前保存并核对的官方文件；目标固件上的 Socket 行为与速度尚未实测。
- 2-64 页的 `FORMat`及 2-188 至 2-189 页的 `TRACe:DATA?`描述 ASCII 和 REAL32。601 点 REAL32 的有效数据为 2404 字节，另有块头/终止符；手册 ASCII 示例约 9 KB。现有适配器已经解析两种格式，并尊重实际格式，没有为每帧强行切换格式。
- 没有据此确认多个查询用分号拼接后的回复格式和原子性。应用队列、一个报文内的复合查询、多个在途请求是三种不同能力，必须分别验证。

原始 TCP 对比仍优先使用 PyVISA 的 SOCKET 资源，复用库存身份和全局锁。Socket 没有与 VXI-11 相同的消息结束语义，必须先验证文本终止符、二进制块长度、尾部读取和超时后状态；不能只改资源字符串就认为传输兼容。地址来自选定资产当次核验，不能硬编码，也不能用备用传输自动重放执行结果未知的命令。

[PyVISA 二进制读取文档](https://pyvisa.readthedocs.io/en/stable/introduction/rvalues.html)说明了类型、字节序、块头、终止符与分块读取。它给出的默认块大小为 20 KiB，高于上述单帧数据量；仅增大缓冲区不足以解释或保证解决多秒延迟。关闭终止符等待也必须有目标设备证据。

## 对照本项目：已知事实与待验证推断

本次核对的[适配器](adapter.py)每帧执行前后两次 `FRAME_SETTINGS`：23 个字段中固定点数无仪器查询，故被动采集为 **22 + 1 + 22 = 45 次串行查询**。现有检查保证了较完整的配置一致性，但也把整套参数轮询放进了每帧路径。

因此，减少重复往返是优先测量的优化方向。不能由查询数量推出 45 倍提速，也不能把全部延迟归咎于 VXI-11、TCP 延迟确认或 ASCII；目前缺少逐命令计时。仪器屏幕更新率也不等于远程可取得的独立新扫描率。

## 后续实施与验收顺序

以下是候选工作，尚未实施；不能当作当前能力或实测结论。

1. **先补计时依据。** 在公共传输/采集诊断中记录命令类别、次数、发送/等待读取/解析耗时、回包字节数，以及整帧、调度和保存耗时。仅运行目录保存目标通信证据，不自动记录完整轨迹。先确认最慢的命令和累计开销。
2. **保持传输不变建立基线。** 在同一连接、同一扫描配置下，分别测量单次轨迹传输、完整快照和当前完整采集。轨迹传输微基准只比较字节数、结构和哈希，不把缺少轴核验的数据送到用户页面。
3. **明确数据与状态的刷新职责。** 型号包列出哪些字段影响本帧坐标、单位、有效性和随帧元数据；这些字段仍须有可靠的新鲜度与一致性证明。身份及其他诊断信息可另行调度。拟缓存字段必须说明软件设置、Auto、重连和前面板变化时如何失效；无可靠检测方式的关键字段继续核验。延迟刷新的设置必须标明来源和时间，不能冒充本帧实际值。
4. **独立比较传输。** 完成消息边界验证后，串行对比现有 VXI-11 与厂家 TCP 5555，在相同格式、相同查询序列下计时，避免同时改变多个条件。分号复合查询另行验证支持性、回复个数、字段归属和失败边界；不引入并发查询。
5. **单独比较数据格式。** 若比较 REAL32 与 ASCII，先备份实际格式/字节序，完成后恢复并读回。数据格式切换属于写入，不混入纯只读基准；继续检查 601 点、块长度、有限值、坐标和单位。
6. **同时验收速度与正确性。** 建议各条件至少连续 20 帧记录中位数、P95、最大耗时及无效帧数；注明是传输完成率还是经证明的新扫描率。覆盖前面板变更、Auto、零跨度、断线重连、保存开启/关闭，确认旧轴、旧缓存或错误帧不会显示/保存。未取得目标实测前不承诺刷新率。

公共代码只承担计时、会话串行、调度与缓存生命周期接口；哪些字段可缓存、使用何种命令及端口能力的判断仍在型号包。源代码研究不替代目标固件的实机验收。

## 2026-09-25 后续采用结果

按用户追加要求，当前生产监测取消周期及逐帧参数查询，首次和失效后完整核对，其余只读曲线；连续记录沿用本次快照并锁定软件参数，纯监测可编辑。旧 45/23、三事务及 30 秒更新数字是研究阶段基线，当前契约见型号 README。

已实机对比 ASCII 与 REAL32：完整采集和纯轨迹路径各 20 帧，二进制明显降低曲线传输耗时；幅度单位、601 点、轴及格式/字节序检查通过，原参数恢复通过。用户确认正式连接 DSA815 默认启用 REAL32，并将“验证后优先二进制”用于后续型号；不会照搬示例的字节序，也不把二进制等同于非 VISA。实测时间分布及失败历史仅在运行目录。连接默认策略单独验证，扫描/只读预检不执行格式设置。

## 流水线与传输复核

官方 [R&S 性能优化说明](https://www.rohde-schwarz.com/us/driver-pages/remote-control/speed-optimization_231252.html)强调区分软件开销与仪器测量耗时，不能为速度牺牲同步。本轮分别测量通信、显示通知和文件转换；读取下一帧可与绘图及保存上一帧并行，但不允许同设备多个未完成查询。

按厂家确认的 TCP 5555，已串行比较 VISA SOCKET 与原生 TCP，同格式、同标量查询序列下的完整和缓存读取各 20 帧。两者稳定帧耗时接近，而 VXI-11 较慢；这支持“主要差异来自协议/后端路径，而不是 Python VISA 封装本身”的判断，不证明所有设备都一样。VXI-11 的成组查询仍单独列为生产基线，不能把 45 次标量查询与 3 次成组事务混算传输改善。TCP_NODELAY 未达到既定完整采集改善门槛，未改生产默认。

两个 Socket 候选的多行选件表结束边界仍未建立完整备份资格，不因被动轨迹速度较快就提升为默认。当前继续使用已验证的 VXI-11/REAL32 和单查询缓存帧；候选比较数据、固件、后端、恢复差异和失败历史在正式运行目录。公共流水线的缓存/记录来源检查保持不变，文件保存和显示的改善来自隔离 Mock 对照，见[软件验收](../../../../../docs/reviews/acquisition-performance-review.md)。
