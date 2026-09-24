# 协议依据与固件差异

来源：[R&S SMA100B 用户手册，第 14 版](https://cdn.rohde-schwarz.com.cn/pws/dl_downloads/pdm/cl_manuals/user_manual/1178_3834_01/SMA100B_UserManual_en_14.pdf)。以下页码为手册印刷页码。实机校验固件为 `4.60.112.29 SP6`；新固件和不同选件组合需单独验证。

| 功能 | 命令/约束 | 手册位置 |
| --- | --- | --- |
| 身份与选件 | `*IDN?`、`*OPT?`，连接时核实完整型号和序列号 | Common commands |
| 动态范围 | `SOUR:FREQ? MIN/MAX`、`SOUR:POW? MIN/MAX`、扫频端点 MIN/MAX | SOUR:FREQ / SOUR:POW 子系统 |
| AM | `SOUR:AM1:STAT/SOUR/DEPT`；百分比深度要求 `SOUR:AM:TYPE LIN` | pp.616–619 |
| FM | `SOUR:FM1:STAT/SOUR/DEV`；单位 Hz，当前模式上限再次查询 | pp.627–630 |
| PM | `SOUR:PM1:STAT/SOUR/DEV`；默认单位 rad | pp.631–634 |
| 调制源 | INT→LF1，EXT→EXT1；两个路径的偏差耦合需先核实 UNC | AM/FM/PM SOUR 与 DEV:MODE |
| LF 发生器 | `SOUR:LFO1:FREQ/SHAP`，`SOUR:LFO:FREQ:MODE CW` | pp.710–717 |
| 物理 LF 输出 | `SOUR:LFO1:STAT?`、`SOUR:LFO2:STAT?`；测试不写 | p.715 |
| 扫频 | `SOUR:SWE:FREQ:MODE MAN`，端点/间隔/驻留/步进；LOG 步进显式 PCT | RF sweep 子系统 pp.761–769 |
| 列表 | `SOUR:LIST:CAT/SEL/FREQ/POW`、`*:POIN?`、DWEL、IND、DEL | §15.18.8 pp.723–737 |
| 文件备份 | `MMEM:DATA? '<当前列表完整路径>'`，IEEE 二进制分帧 | pp.500–501 |
| 参考源 | `SOUR:ROSC:SOUR INT/EXT`；读回不证明参考锁定 | ROSC 子系统 |

当前现场确认以下差异，保留原始失败批次作为证据：

- 未配置的第二内部发生器 `SOUR:LFO2:FREQ?` 无响应；本实现只控制 LF1，并只读检查两路物理辅助输出，不探测 LF2 频率。
- 旧用户列表的 `DWEL:LIST:POIN?` 非零，但 `DWEL:LIST?` 返回空响应。不能把它当成完整数组，改用完整 `.lsw` 二进制备份及哈希保护原数据；频率和功率另行逐点读回。
- RF OFF 时进入 LIST 报 `-221`、包含 `list mode vs. power, RF is off`。只对已证实的固件版本预检拦截，不泛化为型号不支持 LIST。列表上传/选择/驻留与触发执行分开记录。
- 原始设置可能已超过 PEP/功率限幅。恢复原值后可能返回设置冲突，停止写入并通过完整只读快照核实；不改功率限幅来消除错误。
- EXT 参考源可能报告 `-300`、`Reference PLL unlocked`。不隐藏诊断，不以参考源设置成功宣称已锁定。

选件门控使用真实 `SMAB-K720`、`SMAB-K22`、`SMAB-K24`，不使用配置中虚构的功能名扩大连接后的能力。未实测的外部调制、第二内部发生器、非正弦波形和触发执行均保留为未验收。
