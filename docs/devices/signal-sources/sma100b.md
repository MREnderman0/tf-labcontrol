<!-- Generated from an independent device package; edit its README.md. -->

# R&S SMA100B

注册标识：`rs.sma100b`。本目录独立维护射频信号源协议、动态范围、测试和说明。唯一逻辑通道为 1，功率单位为 dBm；不提供函数发生器的 Vpp 幅度或偏置接口。

## 能力与协议

`adapter.py` 定义频率、功率、输出、扫频、AM/FM/PM、参考时钟和频率/功率列表。连接后以真实选件为准：AM/FM/PM 需要 `SMAB-K720`，脉冲调制需要 `SMAB-K22`；未安装 `SMAB-K24` 时内部波形只读为正弦，不开放切换。

- 离线保守范围为 8 kHz…3 GHz、−120…18 dBm；连接后通过 `*OPT?` 与已验证的 MIN/MAX 查询更新频率、扫频端点、功率可设范围。
- 范围快照完整读取后原子发布。刷新失败保留历史值但标记失效、禁止使用过期范围写入；直接使用适配器的调用方也保留写入前刷新。
- 实验配置的限值只能缩小范围。RF 输出限制与功率偏移单列展示，查询不修改它们；可设电平不等同于实际 RF 功率。
- 列表数组使用动态范围与实验限值的交集，另限频率不低于 300 kHz、频率/功率长度一致且为 1…10000 点。不会继续受离线 3 GHz / 18 dBm 上限约束。只允许创建新名称，完整读回点数、顺序和值；不覆盖已有列表，不自动触发执行。
- AM 深度使用线性百分比，FM 使用 Hz，PM 使用 rad。公开 `INT/EXT` 映射到路径 1 的 `LF1/EXT1`；修改深度/频偏前核实路径未耦合，FM/PM 再查询当前模式的偏差上限。
- AM/FM/PM 的内部频率和波形共用 LF1。修改前检查 LF 工作模式、其他调制路径及两个辅助输出；存在共享影响时拒绝写入。未把共享发生器表示为三套独立资源。
- 扫频端点校验顺序，线性步进不得超过跨度；对数步进显式发送 `PCT` 单位。非自动实测使用 `MAN` 模式，不发触发命令。

依据：[官方中国站 SMA100B 用户手册第 14 版](https://cdn.rohde-schwarz.com.cn/pws/dl_downloads/pdm/cl_manuals/user_manual/1178_3834_01/SMA100B_UserManual_en_14.pdf)。逐项命令出处及当前固件差异见[协议说明](../../../src/lab_control/instruments/devices/rs_sma100b/hardware/protocol-notes.md)。手册覆盖范围不等于当前资产的已安装功能。

## 连接与驱动

LAN、USB 或实际安装的 GPIB，优先 PyVISA 和现有 x64 VISA；保留显式配置 TCP Socket 的兼容路线。只使用确认过的单设备资源与预期序列号，连接配置和驱动均留在运行环境。

## 文件与公共依赖

`__init__.py` 提供设备定义/文档元数据，`adapter.py` 包含全部型号命令与动态查询；`tests/` 包含范围、失效、会话隔离和相关界面集成测试；`hardware/readonly.py` 提供只读复核，`hardware/validate_control.py` 提供显式运行的 RF OFF 验证，详见[硬件入口说明](../../../src/lab_control/instruments/devices/rs_sma100b/hardware/README.md)。

`tests/fixtures/capabilities-before.json` 保留迁移前基线；`capabilities-current.json` 记录本轮选件门控、路径 1 命令映射、共享 LF 控制、动态列表范围和扫频操作扩展后的契约。真实通信记录不进入源码。

只依赖公共领域类型、SCPI 基类和通用硬件记录工具。型号工厂、文档生成与导入不加载 VISA、不打开仪器，也不导入其他型号包。

## 测试入口与实机状态

```powershell
.venv-dev\Scripts\python.exe -m pytest src/lab_control/instruments/devices/rs_sma100b/tests -q
.venv-dev\Scripts\python.exe -m lab_control.instruments.devices.rs_sma100b.hardware.readonly --config '<运行目录内单设备配置>'
```

2026-09-24，固件 `4.60.112.29 SP6`、K720 资产在 RF OFF 下已验证基础频率/功率、线性/对数扫频、内部正弦 AM/FM/PM、INT 参考设置、两组列表完整上传/读回、驻留时间与清理、30 轮交替读写及原状态恢复；恢复后 600 次间隔至少 1 秒的连续读取、5 次重连均通过。完整证据及历次失败/受阻记录保留在运行目录资产批次中。

已知限制：该固件在 RF OFF 时进入 LIST 返回 `-221` 设置冲突，兼容层在写入前明确拦截；不因此开启 RF。原始功率可能高于原限幅，恢复该原值会报告 PEP/限幅冲突；保留错误后停止写入，只有完整快照和列表哈希一致才记“状态恢复核实通过”。原参考源为 EXT 时曾出现 PLL 未锁定告警，控制测试使用 INT，最终恢复 EXT；设置读回不代表锁定或精度通过。

当前资产无 K22/K24，相应脉冲调制和非正弦内部波形未实测。实际 RF 输出、频谱/功率精度、外部调制、触发执行、长期稳定性未验收。Mock 范围、故障与界面测试不替代实机测量。

## 扩展与移除

遵循[设备模块规范](../../architecture/device-modules.md)，将新选件和命令保留在本包。撤销注册并删除本目录后重新生成文档，保留用户配置和所有历史验收记录。

## 注册能力（自动生成）

范围、单位及命令来自当前注册定义；资产级实测结果见运行目录。

| 操作 | 参数 / 单位 / 范围 | 作用域 | 方向 | 额外确认 | 状态 |
| --- | --- | --- | --- | --- | --- |
| `frequency` | value: number Hz [8000, 3000000000.0] | 整机 | 读/写 | 无需 | 已实现；待实机 |
| `power` | value: number dBm [-120, 18] | 整机 | 读/写 | 无需 | 已实现；待实机 |
| `frequency_mode` | value: enum  CW/SWE/LIST | 整机 | 读/写 | 无需 | 已实现；待实机 |
| `sweep_start` | value: number Hz [8000, 3000000000.0] | 整机 | 读/写 | 无需 | 已实现；待实机 |
| `sweep_stop` | value: number Hz [8000, 3000000000.0] | 整机 | 读/写 | 无需 | 已实现；待实机 |
| `sweep_spacing` | value: enum  LIN/LOG | 整机 | 读/写 | 无需 | 已实现；待实机 |
| `sweep_dwell` | value: number s [0.001, 100] | 整机 | 读/写 | 无需 | 已实现；待实机 |
| `reference_clock` | value: enum  INT/EXT | 整机 | 读/写 | 无需 | 已实现；待实机 |
| `output` | value: boolean  [None, None] | 整机 | 读/写 | 需要 | 已实现；待实机 |
| `am_enabled` | value: boolean  [None, None] | 整机 | 读/写 | 需要 | 已实现；待实机 |
| `am_source` | value: enum  INT/EXT/LF2/EXT2/NOIS | 整机 | 读/写 | 无需 | 已实现；待实机 |
| `am_frequency` | value: number Hz [0.1, 1000000.0] | 整机 | 读/写 | 无需 | 已实现；待实机 |
| `am_shape` | value: enum  SIN/SQU/TRI/TRAP/PULS | 整机 | 读/写 | 无需 | 已实现；待实机 |
| `fm_enabled` | value: boolean  [None, None] | 整机 | 读/写 | 需要 | 已实现；待实机 |
| `fm_source` | value: enum  INT/EXT/LF2/EXT2/NOIS | 整机 | 读/写 | 无需 | 已实现；待实机 |
| `fm_frequency` | value: number Hz [0.1, 1000000.0] | 整机 | 读/写 | 无需 | 已实现；待实机 |
| `fm_shape` | value: enum  SIN/SQU/TRI/TRAP/PULS | 整机 | 读/写 | 无需 | 已实现；待实机 |
| `pm_enabled` | value: boolean  [None, None] | 整机 | 读/写 | 需要 | 已实现；待实机 |
| `pm_source` | value: enum  INT/EXT/LF2/EXT2/NOIS | 整机 | 读/写 | 无需 | 已实现；待实机 |
| `pm_frequency` | value: number Hz [0.1, 1000000.0] | 整机 | 读/写 | 无需 | 已实现；待实机 |
| `pm_shape` | value: enum  SIN/SQU/TRI/TRAP/PULS | 整机 | 读/写 | 无需 | 已实现；待实机 |
| `am_depth` | value: number % [0, 100] | 整机 | 读/写 | 无需 | 已实现；待实机 |
| `fm_deviation` | value: number Hz [0, 1000000.0] | 整机 | 读/写 | 无需 | 已实现；待实机 |
| `pm_deviation` | value: number rad [0, 20] | 整机 | 读/写 | 无需 | 已实现；待实机 |
| `pulse_enabled` | value: boolean  [None, None] | 整机 | 读/写 | 需要 | 缺选件/配置 |
| `list_mode` | value: enum  AUTO/STEP | 整机 | 读/写 | 无需 | 已实现；待实机 |
| `list_trigger_source` | value: enum  AUTO/SING/EXT | 整机 | 读/写 | 无需 | 已实现；待实机 |
| `list_dwell` | value: number s [0.001, 100] | 整机 | 读/写 | 无需 | 已实现；待实机 |
| `list_trigger` | 无 | 整机 | /写 | 需要 | 已实现；待实机 |
| `upload_list` | name: text  [None, None]; frequencies: array  [300000, 3000000000.0]; powers: array  [-120, 18] | 整机 | /写 | 需要 | 已实现；待实机 |
| `sweep_mode` | value: enum  AUTO/MAN/STEP | 整机 | 读/写 | 无需 | 已实现；待实机 |
| `sweep_step_linear` | value: number Hz [0.01, 3000000000.0] | 整机 | 读/写 | 无需 | 已实现；待实机 |
| `sweep_step_log` | value: number % [0.01, 100] | 整机 | 读/写 | 无需 | 已实现；待实机 |
