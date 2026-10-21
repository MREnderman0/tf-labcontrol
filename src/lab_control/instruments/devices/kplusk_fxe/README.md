# K+K FXE

## 能力与协议

本型号包接入 K+K Messtechnik FXE 计数器的官方 K+K Library 21.2.2 与 Python wrapper 2.1。兼容层使用官方 `Multi_GetReportStruct` 读取完整二进制回报：`RT_DOUBLE` 且报告头在 `0x2000..0x3FFF` 时按小端 `double` 解码为 Hz，最多保留 24 个通道，并跳过消息、时间戳和心跳回报。设备处于官方相位平均模式时，`RT_PHASE` 报文按小端整数周期数和小端小数周期解码，用相邻设备时间戳的增量计算只读 Hz，并在批次元数据中标记为 `derived_from_phase`。状态页仍保留原始报告字符串；兼容层只读，不提供参数写入、自动设置或命令重放。

设备监测页位于“实时监测 → 频率计数器”。用户可勾选要显示和记录的通道，页面提供逐帧实时数值、趋势曲线、当前帧导出和连续记录；读取节奏、历史和写盘由公共监测服务负责。没有完整有效的二进制频率回报时，当前帧不会显示或保存，并把本次采集标为超时/解析失败。

## 连接与驱动

使用 `transport = "kplusk"`，`resource` 填设备 IP（也接受 `kplusk://IP[:port]`）。设备通过交换机接入时，先用 USB 和官方 `KK_FirmwareUpload` 配置静态地址，再拔掉 USB、接入同一实验网段的交换机；兼容层直接调用官方库打开 `<IP>[:<Port>]`。扫描和连接不修改网卡或设备设置。USB 仅用于官方配置工具，不作为运行时协议连接。

官方材料、版本、哈希、许可和换机步骤见 `deployment/packages/tools/kplusk/README.md` 及部署文档。官方条款允许集成到本项目分发的产品/设备，但保留 K+K 权利；官方文件未签名，发布前仍需完成组件复核。

## 换电脑操作清单

按下面顺序迁移一台 FXE。清单中的“自动配置”指项目生成本机网络检查和方案；FXE 的静态 IP 写入仍必须在官方 USB 工具中由用户确认，项目不会把旧电脑的网卡绑定、租约或防火墙规则直接复制到新电脑。

### 准备和备份

- [ ] 停止采集、监测和其他 K+K 软件，确认没有占用设备的进程或硬件锁。
- [ ] 从运行目录备份 `inventory.sqlite3`、设备角色台账和最近一批 `hardware-tests`；保留完整型号、序列号、目标地址、掩码、网关、CAN ID 和最近验证批次。
- [ ] 在新电脑安装项目离线包，先执行 `maintenance/launch-kplusk-config.ps1 -VerifyOnly`，确认官方配置工具和运行时哈希通过。

### 用 USB 写入设备的静态地址

- [ ] 先只接 USB，打开 `KK_FirmwareUpload`，按完整型号和 USB 序列号确认目标设备；记录写入前的 IP、掩码、网关、Firmware、CAN ID、Extended Format 和 Nickname。
- [ ] 只勾选 `Update Configuration`，填写该设备角色的静态 IPv4、掩码和网关；没有上级路由的隔离交换机使用 `0.0.0.0` 网关。不要勾选 Firmware/Bootloader，也不要顺手修改 CAN 或报告设置。
- [ ] 点击一次 `Start Update`，等待完成和设备重启；保存写入前后截图或配置导出。失败、弹窗和中途停止都要写入本批次记录。

### 按网线接入交换机

- [ ] 设备重启后拔掉 USB，用网线接到实验交换机；新电脑选择已连接的物理 Ethernet 网卡，并确认它与设备处于同一网段。不要为了扫描临时改 WLAN 默认路由或系统代理。
- [ ] 先用项目的只读命令检查本机网卡并生成方案：

  ```powershell
  lab-control-cli network inspect
  lab-control-cli network plan --interface "本机实验网卡名称" --plan-file lab-network-plan.json
  lab-control-cli network validate --plan-file lab-network-plan.json
  ```

- [ ] 检查目标地址未被占用后，在项目连接配置中填写 `transport = "kplusk"`、`resource = "<设备 IP>"` 和 `expected_serial = "<序列号>"`。不要使用 `TCP:<IP>:<Port>` 代替直连；那表示 K+K TCP server。
- [ ] 先做 ARP/Ping 等基础检查，再运行 K+K 只读连接和一次重连；只读结果、完整身份、配置读回和物理输出分别留档。

### 更换设备或重复换机时的自动化边界

- [ ] 先按 USB 序列号和完整型号匹配设备角色，再检查目标静态地址是否空闲；旧设备仍在线时禁止给替换设备写入同一地址。
- [ ] 自动化入口只生成网卡检查、地址冲突检查和待审阅配置方案；由用户确认后才调用官方工具写入 IP、掩码和网关。
- [ ] 设备重启后重新用 USB 读回完整配置，再用官方 Library 通过交换机直连；身份、地址和重连均通过后才更新运行目录台账和 `inventory.sqlite3`。
- [ ] 新电脑只迁移设备角色、历史记录和脱敏的官方材料哈希；不迁移旧网卡 GUID、MAC 绑定、DHCP 租约、防火墙规则或绝对路径。
- [ ] 若官方 Library 未返回序列号或完整 FXE 变体，批次必须标记为 `blocked`/`needs_review`，不能按 IP 或目录名自动合并资产。

## 测试与限制

型号专属离线测试在 `tests/`，显式实机只读入口在 `hardware/readonly.py`。测试覆盖二进制 `RT_DOUBLE`/`RT_PHASE` 解码、相位增量、通道选择、无效帧拒绝、连接和重连；实机入口另外记录频率回报和解析后的通道值。测试通过只说明官方库网络连接和回报读取可用；完整型号、序列号、配置读回、恢复和物理输出仍按运行目录批次判定。真实地址、序列号和原始通信不写入源码。

实机测试前置条件和留档入口见 [hardware/README.md](hardware/README.md)。

替换设备时为新资产保留新序列号和地址，先 USB 配置官方工具，再在项目配置中更新 `resource` 与 `expected_serial`，重新执行只读身份/重连批次；不能把旧 IP 或旧资产身份自动继承给新设备。
