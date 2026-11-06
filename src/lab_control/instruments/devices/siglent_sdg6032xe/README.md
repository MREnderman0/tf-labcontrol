# Siglent SDG6032X-E

注册 ID：`siglent.sdg6032xe`，双通道。精确匹配 `SDG6032X-E`；`SDG6032X` 保留自己的独立模块，不将其他 X-E 型号作为别名。

## 能力与协议

当前提供 SINE / SQUARE / RAMP / DC 波形选择，频率、Vpp 幅度、偏置、相位及需显式确认的输出开关。负载、调制、扫频、突发、耦合、参考时钟和相位模式只读。扫描和连接不修改输出、格式或错误队列。

依据：[编程手册 EN06A](https://siglentna.com/wp-content/uploads/dlm_uploads/2025/12/SDG6000X_ProgrammingGuide_EN06A.pdf) §§3.1–3.4、3.10–3.12、3.15、3.22–3.23；[X-E 数据手册 CN06A](https://www.siglent.com/wp-content/uploads/2025/03/SDG6000X_E_DataSheet_CN06A.pdf) pp.8–11；[X/X-E 用户手册 CN02C](https://www.siglent.com/wp-content/uploads/2025/09/SDG6000X_UserManual_CN02C.pdf)。样本为脱敏协议夹具，不是原始实机记录。

## 连接与驱动

优先 PyVISA + LAN VXI-11；USBTMC 与显式 VISA SOCKET 属于厂家接口，尚需独立实机验收。复用 Windows 10/11 x64 组件 `rs-visa`，版本、官方来源、安装材料、哈希/签名、检测、权限及恢复边界见 [组件清单](../../../../../deployment/components.json)。此次没有新增或升级 Python 包、驱动、DLL、服务或安装材料，不要求重新安装厂商驱动。

现场地址、真实序列号、固件和通信保存在运行目录。资产由完整身份绑定，LAN 地址可跟随重新核验的发现结果；不修改网卡、DHCP 或防火墙。扫描不能代替单台参数测试。

## 单位与限制

| 项目 | 当前支持范围与组合约束 |
| --- | --- |
| 频率 | 最小 1 μHz；SINE ≤350 MHz，SQUARE ≤120 MHz，RAMP ≤5 MHz；DC 不提供频率 |
| 幅度 | Vpp；高阻最小 2 mVpp，50 Ω 减半；正弦上限按 ≤40 / ≤120 / ≤160 / ≤350 MHz 分为 20 / 10 / 5 / 3 Vpp，高阻方波在 20 MHz 以上限为 10 Vpp；50 Ω 上限减半 |
| 偏置 | V；保守限制 `幅度/2 + abs(偏置) ≤ 当前频段幅度上限/2`，同时保留设备 `MAX_OUTPUT_AMP` 限制；不静默钳位 |
| 相位 | 0–360°，DC 不适用 |
| 波形与输出 | 切换波形前必须关闭该通道输出；输出开启需显式确认；输出关闭不受高级模式与耦合门禁阻止 |
| 独立控制 | 跟踪及频率/幅度/相位耦合必须关闭；调制、扫频、突发必须关闭；只支持已核实的高阻或 50 Ω 写入约束 |
| 异常 | 缺字段、错误通道、重复字段及非有限数拒绝；超时不重发写入，状态未知须显式重连并核验 |

BSWV 的 AMP 始终按 Vpp 解释，不依赖前面板显示单位。读取完整基础回包后更新状态；DC 或其他模式缺少的参数不伪造数值。

## 文件与公共依赖

`adapter.py` 保存型号能力、范围和写入前校验；`protocol.py` 解析本型号回包。`tests/fixtures` 保存初始软件契约及脱敏样本。`hardware` 保存只读/控制入口、测试计划与记录转换器。仅通过 `devices/catalog.py` 注册，不交叉导入其他型号；公共层负责会话、进程/资源锁、传输、库存和离线台账。

## 测试入口与实机状态

执行前核对[本型号硬件计划](hardware/README.md)中的前置状态、备份、停止及恢复顺序。

```powershell
.venv-dev\Scripts\python.exe -m pytest src/lab_control/instruments/devices/siglent_sdg6032xe/tests
.venv-dev\Scripts\python.exe -m lab_control.instruments.devices.siglent_sdg6032xe.hardware.readonly --config '<运行目录中的单设备配置.toml>'
.venv-dev\Scripts\python.exe -m lab_control.instruments.devices.siglent_sdg6032xe.hardware.control --config '<运行目录中的单设备配置.toml>' --write
```

| 功能 | 接口及条件 | 验证范围 / 状态 | 证据位置 |
| --- | --- | --- | --- |
| 身份、范围、错误回包、超时不重放、恢复失败 | 离线严格协议与运行器替身 | 软件回归覆盖；不是实机故障注入 | 包内 tests |
| 枚举、身份、正式会话、双通道读回 | LAN VXI-11，固件 6.01.01.38R3 | 已完成一次资产接入验证 | 运行目录 hardware-tests 的资产状态和批次 |
| 频率、幅度、偏置、相位 | 双通道 SINE、高阻、输出关闭 | 每通道 4 项写读及完整快照恢复、通道隔离通过 | 运行目录原始 results.json / communication.jsonl |
| 重连 | 关闭会话后重新打开 | 三次身份与参数一致 | 运行目录只读和控制批次 |
| 物理输出与精度 | 尚未确认外部测量接线与参考仪器 | not_tested，不作为整机验收通过 | 运行目录 summary.json |

## 已知限制与待测

| 功能或问题 | 限制 / 原因 | 后续验证 |
| --- | --- | --- |
| 输出开启、波形切换与带宽边界 | 有软件范围/确认保护，本次未执行实机开输出或切换波形 | 明确接线、负载和参考仪器后另批测试 |
| PULSE / ARB / NOISE / PRBS / MULTPULSE / SEQUENCE / IQ | 可识别现态，当前基本写入拒绝这些模式；未实现专属时序、存储和组合校验 | 在本包扩展协议与状态备份后测试 |
| 调制、扫频、突发、波形上传、负载和参考时钟写入 | 只开放已验证的基本控制范围，未注册这些写入能力 | 独立计划、授权和恢复证据 |
| USB / SOCKET / 其他固件 | 本次只验证 LAN VXI-11 与一个固件 | 逐接口、逐固件验收，不合并为全部通过 |

## 扩展与移除

修改本包及唯一注册，运行生成器刷新派生文档。保留原能力基线，新增功能另存当前契约。移除模块后保留用户库存、连接配置与实机历史，旧配置应明确提示适配器不可用。
