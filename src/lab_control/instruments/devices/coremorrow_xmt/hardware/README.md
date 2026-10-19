# 芯明天 E00 / XMT 实机测试计划

目标资产由现场显式指定一个 COM/VISA ASRL 资源。只读入口先检查设备枚举和正式运行目录，再通过 `coremorrow.xmt` 兼容层读取配置和每个已配置通道的原始帧，最后释放并重连一次。可逆控制入口在写入前备份三个通道，逐通道发送一次 0.25 V、读回并恢复原值。

只读入口不发送设置、校准、清零、波形或输出命令，不读取未确认单位的物理量，也不把 `VID_0483&PID_0002` 或 USB 桥序列号当作仪器身份。控制入口开放 E00.C3-13 的 0.0～120.0 V 非负控制子范围；写入失败或读回不确定时不自动重发，也不声称已恢复。两种入口都不把软件读回当成物理输出测量。

```powershell
.venv-dev\Scripts\python.exe -m lab_control.instruments.devices.coremorrow_xmt.hardware.readonly `
  --resource COM6 --asset-name COREMORROW-XMT-UNKNOWN-COM6
```

```powershell
.venv-dev\Scripts\python.exe -m lab_control.instruments.devices.coremorrow_xmt.hardware.validate_control `
  --resource COM6 --asset-name COREMORROW-E00-C3-13-COM6
```

资产目录名建议使用不含空格的人工编号，例如 `COREMORROW-XMT-UNKNOWN-COM6`。真实地址、序列号和通信记录只写运行目录 `hardware-tests`。

## 标准波形测试

`validate_waveform.py` 要求显式指定目标 COM、已核对的 USB 桥序列号和三路空载条件；桥序列号仅核对连接资产，不冒充仪器序列号。遵守正式进程与资源锁，遇占用留档退出，不结束其他进程。测试前保存配置和三路电压；模式无法读回则明确记为未知。

```powershell
.venv-dev\Scripts\python.exe -m lab_control.instruments.devices.coremorrow_xmt.hardware.validate_waveform `
  --resource '<选定 COM 端口>' --asset-name '<已有资产目录>' --expected-usb-serial '<已核对的桥序列号>' --unloaded
```

| 项目 | 操作与判定 |
| --- | --- |
| 初始条件 | 三路电压必须落在可恢复的 0～120 V 范围（近零误差按既有 0.01 V 容差处理）；显式停止后若读数与备份不符，终止波形测试 |
| 单路波形 | 每路依次发送正弦、方波、三角、锯齿：1 Hz、0.2 Vpp、偏移 0.1 V；每次只写一帧，然后停止并恢复 |
| 参数调整 | 各路发送 2 Hz；再改为 0.4 Vpp、偏移 0.3 V。新测试波形上沿不超过 0.5 V；恢复值使用测试前原值，可能高于 0.5 V |
| 多通道 | 启动三路、单独停止第二路、执行回常规恢复；记录每帧和软件状态，不把通道地址正确当作物理隔离已测 |
| 恢复与重连 | 最终独立读回与初始电压相差不超过 0.01 V；释放再连接只读，无自动恢复输出 |
| 验证边界 | 无波形参数/状态确认回包时，只标命令交付通过；参数独立读回和外部波形测量保持未测 |

每批保留 `results.json`、`summary.json`、原始帧、初始/最终快照，并通过公共维护器更新索引。身份缺项、受阻和恢复失败分别记录；不修改原始旧批次。

