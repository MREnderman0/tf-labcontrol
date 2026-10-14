# 芯明天 XMT 实机只读计划

目标资产由现场显式指定一个 COM/VISA ASRL 资源。测试先检查设备枚举和正式运行目录，再通过 `coremorrow.xmt` 兼容层读取配置和每个已配置通道的原始帧，最后释放并重连一次。

本入口不发送设置、校准、清零、波形或输出命令，不读取未确认单位的物理量，也不把 `VID_0483&PID_0002` 或 USB 桥序列号当作仪器身份。只有厂家确认完整型号、序列号来源、原始数据缩放和可恢复控制流程后，才可在独立批次增加写入或物理输出测量。

```powershell
.venv-dev\Scripts\python.exe -m lab_control.instruments.devices.coremorrow_xmt.hardware.readonly `
  --resource COM6 --asset-name COREMORROW-XMT-UNKNOWN-COM6
```

资产目录名建议使用不含空格的人工编号，例如 `COREMORROW-XMT-UNKNOWN-COM6`。真实地址、序列号和通信记录只写运行目录 `hardware-tests`。

