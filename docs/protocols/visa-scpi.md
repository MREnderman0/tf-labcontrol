# 传输协议与生命周期（0.3.0 复用）

通信实现位于 src/lab_control/transport；设备命令位于 instruments，UI、配置与 CLI 只能提交类别能力命令。

分类控制页和实时监测共用原会话传输。MonitoringService 不创建第二句柄；参数在完整读取之间执行，扫描仍只复用活动会话身份缓存。read_settings 由适配器选择已核实查询或本会话缓存，不能由界面拼接厂家命令。

## 传输选择

| 实现 | 路线 | 边界 |
| --- | --- | --- |
| VisaTransport | USBTMC、VXI-11/HiSLIP、GPIB、ASRL、VISA Socket | 使用已安装的 VISA Runtime；不携带硬件驱动 |
| SerialTransport | 已知 COM/波特率，默认 8N1 | 打开前设置 DTR/RTS 为 false；未知端口不自动打开 |
| TcpSocketTransport | 显式配置的厂家 Socket 地址 | 文本、定长/二进制分帧；不扫描全网 |
| VicpTransport | LeCroy VICP | 8 字节头、版本、序号、DATA/EOI、多包响应 |
| VendorLibrary + SDK 适配器 | TLPMX/PM100D、ps6000a | ctypes 显式参数/返回类型；不虚构 write/query |
| TserveTransport | 53100A 已部署 TSERVE 命令口 | 欢迎信息/提示符与 Telnet 协商；数据口另建只读连接 |
| MockTransport | 严格请求/响应夹具 | 未预期 I/O 立即失败，不加载驱动 |

Transport 保留 open/close/write/read/query。字节传输额外提供 query_fixed，供 GPD 单字节状态使用。SDK 共享领域错误、身份、状态和采集模型。

## VISA 唯一所有者

每个 ApplicationRuntime 持有应用级 VisaRuntime，长期会话和扫描共用。枚举后不关闭资源管理器；所有设备释放后才在退出时关闭。PyVISA 的管理器关闭会影响关联资源，不能在扫描函数 finally 中关闭：[官方生命周期说明](https://pyvisa.readthedocs.io/en/latest/api/resourcemanager.html)。

正确调用已实现的方法：

    resources = app.visa.enumerate()
    # 不在这里 close()；完整扫描调用 app.discovery.scan()

## 消息边界

- write(str) 添加配置的终止符；write(bytes) 原样发送，由适配器提供完整帧。AFG 二进制显式带 LF；SDG WVDT 不追加 LF。
- BufferedReader 处理分包/粘包；IEEE 定长块校验头、长度、截断和 64 MiB 上限。负载内部 CR/LF 不是消息结束符，不对二进制执行 strip。
- 通用流拒绝没有外部 EOM 的 #0 块。有明确 EOM 的完整响应才可允许不定长块解析。
- VISA 原生仪器读取 EOM；ASRL/Socket 使用流式分帧。3458A 按 CR/LF 定界，不需要连接时修改 END OFF。
- GPD STATUS? 按一个状态字节读取；VICP 根据 EOI 拼接并验证报头。
- RIGOL BYTE、LeCroy WAVEDESC、R&S ASCII 和 ZNB 复数轨迹分别解析；字节序、ADC 缩放和坐标轴由型号适配器处理。

## 扫描和资源租约

枚举 provider 与身份探测分离。COM/ASRL、同地址 VISA Socket/直接 TCP 可规范化；USB/LAN/GPIB 跨协议身份通过已核实别名或身份合并确认。打开探测资源前，在会话锁内原子获得资源租约；连接也检查探测门闩。

活动设备只用身份缓存；显式刷新走原 worker。新资源可能指向活动设备时，登记并延后探测。候选先全部进入库存，避免慢设备阻止其他资源显示。provider 并行枚举，身份探测保守串行。详见[扫描与总览设计](../requirements/device-discovery-and-overview.md)。

## 超时和驱动边界

配置 timeout 用于底层 I/O，SDK 轮询、TSERVE/3458A 批次另有总截止时间。第三方驱动阻塞枚举或 DLL 调用无法由 Python 线程强制中断；取消阻止后续探测，等待当前调用返回。软件不会通过终止其他用户进程解锁。

控制超时返回 uncertain/partial，不自动重发；会话进入 degraded 后拒绝排队控制。重连只打开和识别，不复位、不恢复输出和预设。临时探测句柄由扫描自身释放。

PyVISA/PySerial 是 Python 依赖；VISA Runtime、USB-GPIB 驱动、PicoSDK、Thorlabs OPM/DLL 独立安装。版本、位数及授权记录在[实机指南](../hardware-testing.md)，厂商 DLL 不随 wheel 分发。
