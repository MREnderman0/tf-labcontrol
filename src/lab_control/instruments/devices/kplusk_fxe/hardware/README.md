# K+K FXE / FXE2 实机只读计划

本入口只接受用户明确指定的 K+K IP 或 `kplusk://IP[:port]`，通过正式会话、监测页及记录器执行只读连接、实际通道识别、频率读取、默认 60 秒监测与逐帧记录、当前帧保存、部分通道选择和重连。不扫描其他地址，不改网卡，不调用设备设置 API，也不进行物理输出测量。首次采集失败立即留档，不用循环重试掩盖失败。

现场 K+K 保持 DHCP，执行前从有效租约核对当前地址，不能照抄上次 IP。身份使用官方只读版本查询 `0x81` 和 `0x7001 VERSION` 回包核对完整型号及序列号，版本文本中的旧 IP 不参与定位。常规扫描对已登记资产按 MAC 跟随租约，每次仍核对真实身份；本入口不配置 DHCP 或强制设备换租。

```powershell
.venv-dev\Scripts\python.exe -m lab_control.instruments.devices.kplusk_fxe.hardware.readonly `
  <设备 IP> --expected-serial <现场序列号> --duration 60 --input-condition "现场输入和参考接线说明" `
  --folder <正式运行目录>\hardware-tests\<资产号>\<批次>
```

正式安装版提供相同的型号注册入口；先正常退出已运行的主程序，不能绕过进程锁：

```powershell
& '<安装目录>\TF-LabControl.exe' --cli --resource '<设备 IP>' --adapter kplusk.fxe --transport kplusk `
  verify-readonly --duration 60 --input-condition '四路输入悬空，仅检查底噪数据链路' `
  --folder '<正式运行目录>\hardware-tests\<资产号>\<批次>'
```

若需保留资产序列号等配置，另传 `--config <正式配置文件>`。该入口显示正式监测控件并保存截图；二进制和 CSV 记录在运行目录，批次保存路径与帧数核验结果。成功退出只表示本批只读软件链路通过，`hardware_accepted` 始终为 false；完整身份未读回保持 blocked，物理测量和精度保持未测试。

真实地址、序列号和原始报告只写入运行目录。身份缺少官方序列号或完整 FXE 变体时必须记录 `blocked`；兼容层连接通过不代表参数控制、恢复或物理输出验收通过。
