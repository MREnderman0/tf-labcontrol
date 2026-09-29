# DHO924 硬件验证

本入口仅访问配置中一台精确 DHO924，要求预期序列号；不扫描或切换协议。[型号说明](../README.md)是能力维护源，按[新设备流程](../../../../../../docs/testing/onboarding.md)与[留档规范](../../../../../../docs/testing/records.md)执行。

## 配置与授权

在正式运行目录保存一项 `[[devices]]` 配置，填写资产真实 VISA SOCKET 资源、`adapter="rigol.dho924"`、`transport="visa"`、`expected_serial` 和适当超时。源码及命令示例不包含真实地址和身份。四个模拟通道为空输入；写入测试须有用户授权。

```powershell
python -m lab_control.instruments.devices.rigol_dho924.hardware.verify --config <运行目录中的配置文件> --phase readonly
python -m lab_control.instruments.devices.rigol_dho924.hardware.verify --config <运行目录中的配置文件> --phase protocol
python -m lab_control.instruments.devices.rigol_dho924.hardware.verify --config <运行目录中的配置文件> --phase functional
python -m lab_control.instruments.devices.rigol_dho924.hardware.verify --config <运行目录中的配置文件> --phase acquisition
```

## 执行边界

1. 用正式运行目录核对库存和锁的最终物理路径；拒绝 AppData 重定向副本。连接核验型号、序列号；占用即留档受阻，不抢锁、不终止进程。
2. `readonly` 明确跳过采集初始化，备份全部四通道、时基/位置/存储/触发、运行及传输设置，三次关闭重连核对状态。
3. `protocol` 在停止的同一波形上对照 WORD 和 ASCII，保留原始帧、前导信息、浮点数组和误差界限；恢复传输及运行状态。`completion` 是独立的完成同步诊断批次，只检查厂家完成命令，不自动重复设置。
4. `functional` 在保守设置逐项修改、读回、通道隔离、恢复；验证运行/停止/单次等待。然后读取屏幕及完整存储、手动保存、各约 60 秒监测和记录，检查锁、停止排空、二进制/CSV 逐样本一致性。`acquisition` 独立复核采集、监测与保存阶段，仍先备份并在结束后核验恢复。性能记录为功能验收附带观测，不承诺帧率。
5. 异常停止后续测试。读取当前状态后仅做一次有完整备份依据的恢复；通信边界不明或恢复失败则报告最终已知状态/差异，不自动重连重放。按探头、档位、偏移、时基/存储、触发、传输及运行状态的依赖核实恢复。

## 留档

每个批次保存 `results.json`、`summary.json`、`record.md`、通信、初末快照及恢复差异；波形阶段保存原始响应/哈希及对照数组。身份不符、失败、中止和路径/锁受阻同样发布摘要，由公共维护器刷新资产状态、总索引和离线总览；原始批次不重写。CSV 和二进制保存仍使用公共正式流水线。

## 未测试范围

`main_mode.verify_main(config, folder)` 是已授权 MAIN 策略的独立入口：只读连接保存完整快照，关闭后以正式初始化重连；仅当仪器已处于 ROLL 才验证自动退出，不为测试主动进入 ROLL。完成后读取三帧，恢复原传输设置并逐项保存最终差异；有意保留用户要求的 MAIN 和安全时基，不恢复原 ROLL。该批次不声称连续监测、记录或物理精度验收。调用前须核对正式路径和目标身份，结果由同一公共维护器留档。

USB、VXI-11 控制、物理精度、数字/数学/分段及高级分析；空输入不属于整机性能验收。安装版仍须在干净 Windows 10/11 上完成驱动与逐设备交付验收。
