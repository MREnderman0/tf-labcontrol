# K+K FXE 实机只读计划

本入口只接受用户明确指定的 K+K IP 或 `kplusk://IP[:port]`，通过项目 `kplusk.fxe` 工厂执行官方库只读连接、元数据/原始报告读取和一次重连。它不扫描其他地址，不改网卡，不调用设备设置 API，也不进行物理输出测量。

```powershell
.venv-dev\Scripts\python.exe -m lab_control.instruments.devices.kplusk_fxe.hardware.readonly `
  <设备 IP> --expected-serial <现场序列号> --folder <正式运行目录>\hardware-tests\<资产号>\<批次>
```

真实地址、序列号和原始报告只写入运行目录。身份缺少官方序列号或完整 FXE 变体时必须记录 `blocked`；兼容层连接通过不代表参数控制、恢复或物理输出验收通过。
