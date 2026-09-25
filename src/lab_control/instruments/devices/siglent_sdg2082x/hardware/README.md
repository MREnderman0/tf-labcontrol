# SDG2082X USB/LAN 高级控制验收

仅使用单设备配置中的 USB 或 LAN/PyVISA 资源和完整预期序列号，通过项目型号工厂、会话 worker 和固定全局硬件锁执行。先正常退出真实 GUI。Windows 桌面环境执行时核对实际锁文件、配置和库存物理路径，不能使用重定向副本的空闲锁替代正式程序的锁。

```powershell
.venv-dev\Scripts\python.exe -m lab_control.instruments.devices.siglent_sdg2082x.hardware.validate_control --config '<单设备配置>' --preflight-only
.venv-dev\Scripts\python.exe -m lab_control.instruments.devices.siglent_sdg2082x.hardware.validate_control --config '<单设备配置>' --backup-only
.venv-dev\Scripts\python.exe -m lab_control.instruments.devices.siglent_sdg2082x.hardware.validate_control --config '<单设备配置>'
.venv-dev\Scripts\python.exe -m lab_control.instruments.devices.siglent_sdg2082x.hardware.validate_control --config '<单设备配置>' --restore-from '<原批次绝对路径>'
.venv-dev\Scripts\python.exe -m lab_control.instruments.devices.siglent_sdg2082x.hardware.validate_control --config '<单设备配置>' --verify-only
```

第一条仅查询；第二条有受控波形/模式切换，备份后立即恢复；第三条修改参数并恢复；第四条读取同一资产指定批次的备份，先核对当前状态再恢复，不重放原失败操作。
第五条仅复核当前状态、连续读取和重连，零参数设置；用于恢复后的独立稳定性复核。

## 范围与初始条件

枚举、身份和连接分开记录，报告按实际接口生成。预检及重连禁用采集初始化；两路主输出及同步输出必须 OFF，否则停止。

- 两路主输出、同步输出关闭；通道跟踪/耦合关闭。完整测试要求初始高级模式关闭，初始标准周期波形频率不高于 1 MHz；恢复入口允许原失败批次留下的高级模式，但仍要求输出关闭。
- 保存 BSWV、OUTP、MDWV、SWWV、BTWV、ARWV、SRATE、SYNC、COUP 和用户波形清单；模式关闭只返回 STATE 时，受控启用后立即备份所有返回字段。参数写入前保存对应模式/波形的原始值。
- 备份后检查测试会修改的字段全部可读；本入口要求保存的 AM/FM/PM 源为 INT、突发为 NCYC/INT，否则在进入参数矩阵前停止并恢复已有快照，避免覆盖未备份的隐藏值。
- 测试不发送输出开启、立即触发、复位、清空错误、负载设置、同步/触发输出设置或全局系统设置。每步检查两路输出，兼容层操作核验另一通道未变化。
- 双通道基础补测覆盖相位、占空比、脉宽、斜坡对称度及非零偏置；AM/FM/PM 使用 10 kHz、0.2 Vpp 载波和内部正弦调制；扫频 1–2 kHz、LINE/LOG、1/2 s；突发 NCYC/GATE、1/5 周期、MAN/EXT 及延时。
- 固件 2.01.01.35R3B2 在 MAN 下不返回 PRD，测试不通过内部触发补测周期；周期项目单独 not_tested。恢复原 INT 配置时，先恢复原触发源，再核对已备份的 PRD。
- 任意波形上传/读回的软件路径已实现；因缺少本型号远程清理的已核实依据，实机上传组 blocked，零文件上传/删除。不得用同系列其他型号的命令试探。
- 30 轮双通道频率/幅度交替读写，恢复后 60 次间隔至少 1 秒的读取，以及 3 次释放后重连。

## 恢复与证据

所有类别统一结构化维护：最终摘要生成后调用公共 `refresh_after_batch`，更新当前结论、接口矩阵、功能覆盖、已知问题及批次表；不逐次追加现行说明。原始批次和恢复证据保持不变。

恢复遇到首个读回不一致或通信异常，停止进一步写入并保存最终只读快照。高级模式内部设置逐组与备份比较，最后核对整机初始/最终查询快照。只有核验一致才标记恢复通过；单纯写指令成功不算恢复成功。

每次创建独立 `hardware-tests/<型号>-<序列号>/<时间>-<类型>/` 批次，保存 `record.md`、`results.json`、通信日志、代码版本与源码副本；使用批次独立审计库，但仍使用正式全局硬件锁。失败、受阻和恢复批次分别留存并关联，通过公共 `refresh_after_batch` 更新结构化资产说明与去重总台账，不追加文末说明。运行数据和实际设备配置不提交源码。

本入口不验证物理波形、输出准确度、外部调制、数字调制、长期稳定性和网络接口，能力矩阵不得因此标为整机实测通过。

## 离线摘要与历史导入

`records.py` 仅解释本型号记录，不连接设备。测试入口在原始 results.json 保存后更新 summary.json；实机步骤和恢复顺序不因摘要改变。按[统一记录规范](../../../../../../docs/testing/records.md)离线导入并重建总览，旧批次与人工连接说明保持原样。
