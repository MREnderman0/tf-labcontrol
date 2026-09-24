# AFG31252 USB 控制验证

使用 `tek.afg31252`、项目 Python、现有 64 位 VISA 和全局硬件锁。只连接单设备配置中指定的 USB 资源，并核对完整型号和 `expected_serial`；不探测 LAN，不占用其他设备。界面和通用会话接口不变。

## 执行与范围

在项目根目录运行，配置保存在运行目录，不能把真实资源和序列号提交到源码：

```powershell
$cfg = "$env:LOCALAPPDATA\TF-LabControl\config\afg-bench.toml"
.venv-dev\Scripts\python.exe -m lab_control.instruments.devices.tek_afg31252.hardware.validate_usb --config $cfg --preflight-only
.venv-dev\Scripts\python.exe -m lab_control.instruments.devices.tek_afg31252.hardware.validate_usb --config $cfg
.venv-dev\Scripts\python.exe -m lab_control.instruments.devices.tek_afg31252.hardware.validate_ranges --config $cfg
```

第二条命令会修改参数，适用于已授权的输出关闭测试。脚本保存初始参数、负载、相位、脉冲、扫频、调制、突发、触发及联动状态，读取初始错误队列；完整备份 EMEM1/EMEM2 并计算 SHA-256 后才允许写入。备份或恢复能力不完整时阻止写入。

第三条命令在完整回归中增加范围补测：双通道 SIN 200/210/250 MHz、SQU 160 MHz、RAMP 2.5 MHz，原负载为 50 Ω 时补测 1 mVpp。无效频率、幅度/偏置包络、扫频端点和脉宽只传入型号的 `validate_command/preflight`，不进入 `apply`，即使意外放行也停止且不写仪器。逐项记录拒绝理由与设置写入数为零。有限负载变化、高阻、极小频率、其他极限组合只作软件测试，不标成实机通过。

每个批次使用自己目录中的审计数据库，但仍获取同一用户固定的全局硬件锁；不会绕过正在运行的真实设备进程。输出极性等快照枚举异常（包括合并回包）会阻止备份通过；空消息超时仍记为失败，不能以空错误字符串误报成功。

| 分组 | 测试内容 |
| --- | --- |
| 基础 | 双通道 SIN/SQU/RAMP/PULS/NOIS/DC，1/2 kHz、0.1/0.2 Vpp、±0.05 V、0/0.5 rad；负载不变 |
| 脉冲、斜坡 | 1 kHz、25/75% 占空比、250 μs 脉宽；25/75% 对称度 |
| 扫频 | 1–2 kHz、LIN/LOG、1/2 s、CW/SWE 切换 |
| AM/FM/PM | 10 kHz 正弦载波、0.2 Vpp、内部 100 Hz；10/20%、100/200 Hz、10/20°及各模式启停 |
| 突发 | TRIG/GAT、1/5 周期及启停；外部触发源，不发送触发命令 |
| 任意波形 | 各通道不同的 256 点数据、EMEM1/EMEM2 完整读回、编码/量化和存储器隔离、波形选择 |
| 重复性 | 参数交替写读 10 轮；恢复后读取 60 次，间隔 1 s；释放后重连 3 次 |

每次设置检查另一通道和两路输出；任意波形还比较另一块编辑存储器。两路输出始终保持 OFF。脚本拒绝输出开启、触发、复位、清空错误队列命令以及非易失存储操作，不修改用户文件、保存槽或序列。初始错误通过查询保存，不使用 `*CLS`。

## 协议与恢复

- 噪声：应用使用 `NOIS`，仪器接受 `PRNOISE` 并返回 `PRN`。
- 相位：正弦分辨率 0.01°，其他标准波形 0.1°；PM 分辨率 0.1°。判定允许半步量化和弧度文本舍入，报告始终保留实际读回，不能伪造为请求值。相位 0.5 rad 的实际读回可能是 0.50004 rad。
- PM：公共单位为度，明确写入 `DEG`，查询返回弧度后转换为度。
- 编辑存储器：大端无符号 16 位承载 0…16382 编码，读取严格解析 IEEE 块，不去除载荷中的换行字节。备份不完整不上传；恢复逐字节比较并复核 SHA-256。
- 波形选择：明确写入 `SOUR1:FUNC EMEM1` / `SOUR2:FUNC EMEM2`，官方查询只返回通用 `EMEM`。记录命令接受、编辑波形类型和两块存储器隔离，不将类型读回说成物理输出确认；若测试前已选编辑波形，因为无法由该查询确定原始存储器编号，阻止新的控制测试。
- 恢复先读取实际状态，不重放失败命令；先退出高级模式，再恢复存储器和关联参数，最后恢复模式及联动，保持输出 OFF。未改变的参数不重复写入，避免模式约束导致拒绝。任何失败记录实际最终差异，不能把部分恢复标为通过。
- Windows 报告文件被读者短暂占用时，仅对本地文件替换作有界重试；不重发设备命令。永久写盘失败仍报错并留存可取得的恢复结果。

失败批次的初始状态可在人工检查后作为恢复目标，仍需精确匹配同一资产及备份哈希，并为恢复操作新建批次：

```powershell
.venv-dev\Scripts\python.exe -m lab_control.instruments.devices.tek_afg31252.hardware.validate_usb --config $cfg --restore-from '<原批次绝对路径>'
```

依据：[编程手册 077148803](https://www.tek.com/-/media/documents/077148803_afg31000_series_programmersmanual_nov2020.pdf)、[规格手册 077148901](https://download.tek.com/manual/077148901_AFG31000_Spec_PV_Sept2019.pdf)、[数据表 75W-61444-3](https://download.tek.com/datasheet/AFG31000-Datasheet-EN-US-75W-61444-3.pdf)。

## 逐台留档与验收边界

遵循[实机测试记录规范](../../../../../../docs/hardware-test-records.md)。每次新建 `hardware-tests/<型号>-<序列号>/<时间批次>/`，保存 `record.md`、`results.json`、原始通信、初始/最终快照、波形备份及哈希，并更新 `index.md`。失败批次和后续恢复批次分别保留、相互关联。

在资产目录的 `connection-status.md` 中逐接口记录结果及历史排查链接。当前现场结论统一为：**“当前仅 USB 连接验证通过；USB 转网口及原生网口直连均未成功建立通信，LAN 暂不可用，原因未确定。”** 默认配置继续使用 USB。该结论仅针对已测试资产及现场环境，不改变型号的 LAN 能力声明。

基础控制、高级功能、恢复、重复读取和重连分别判定；模型能力清单中的 `hardware_passed=false` 是未做全型号验收的保守标记，具体已验证项目以运行目录证据为准。输出开启、实际波形/精度测量、外部调制、FSK/PWM、序列播放及长时间稳定性不属于本轮。Mock 故障注入与实机证据分开记录。
