# SMA100B 实机验证入口

本包所有实机入口都必须显式提供运行目录内的单设备配置，使用 `rs.sma100b`、VISA 资源及完整预期序列号。通过项目 `ApplicationRuntime`、型号工厂、会话工作线程和全局硬件锁执行。被占用时记录受阻，不换接口、不终止其他进程。

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.venv-dev\Scripts\python.exe -m lab_control.instruments.devices.rs_sma100b.hardware.readonly --config '<单设备配置>' --asset-name '<SMA100B-资产号>'
.venv-dev\Scripts\python.exe -m lab_control.instruments.devices.rs_sma100b.hardware.validate_control --config '<单设备配置>' --asset-name '<SMA100B-资产号>' --preflight-only
.venv-dev\Scripts\python.exe -m lab_control.instruments.devices.rs_sma100b.hardware.validate_control --config '<单设备配置>' --asset-name '<SMA100B-资产号>'
```

最后一条会修改并恢复参数。RF 全程 OFF，测试功率不超过 0 dBm；恢复时仅可写回已备份原值。禁止 RF ON、触发、复位、清空错误、功率限幅/偏移写入和辅助输出切换。每项请求、读回、错误和输出状态均落盘，超时不重发。完整备份失败时不进入控制矩阵。

测试矩阵包括 6 个频率×4 个功率、两种扫频间隔和驻留时间、AM/FM/PM 内部频率与深度/频偏组合、INT 参考、两组各 3 点列表、30 轮基础读写、恢复后 600 次间隔至少 1 秒的读数及 5 次重连。LIST 模式的当前固件限制单独记 `blocked`，列表数据控制单独验收。控制测试使用 INT 参考，最终恢复原值；不会要求新接外部参考。

备份包括基础、扫频、六条调制路径及共享 LF、参考源、列表目录/原选择/索引/驻留设置、原选择列表的完整频率和功率数组、完整 `.lsw` 文件及 SHA256。只创建批次唯一临时列表，只删除本批拥有的名称；先切回原列表并核验数据，再清理临时列表。不会重写用户列表文件。

恢复遇到错误立即停止进一步写入；独立读取完整快照与原列表哈希核实实际状态。只有全部一致才将状态恢复标为通过，并同时保留恢复指令错误；通信失败或差异不为空则明确记录恢复失败/最终状态未确认。只读阶段的设备诊断另存，不能视为物理性能通过。

每次执行新增 `hardware-tests/<资产>/<时间>-<类型>/`，包括 `record.md`、`results.json`、初末快照、列表备份、源文件及哈希、`communication.jsonl`，并追加总台账。失败和只读预检同样留档，不能覆盖或删除旧批次。源代码中不得写入真实序列号、地址或通信数据。
