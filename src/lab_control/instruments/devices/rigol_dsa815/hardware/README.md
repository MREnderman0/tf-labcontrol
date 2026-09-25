# DSA815 LAN 单设备验收

## 只读查询效率对照

```powershell
.venv-dev/Scripts/python.exe -m lab_control.instruments.devices.rigol_dsa815.hardware.benchmark --config <运行目录内的单设备配置.toml> --compare-queries
```

仅使用当前 VXI-11 连接、格式和扫描设置，分别读取逐条/复合查询的 full/live 各 20 帧；随后通过公共监测服务纯监测 60 秒、核对参数未变并释放/重连三次。每次重连再次核对身份与设置。此入口不启动扫描、不发送 Auto、不切换格式或网络设置、不保存完整曲线；保留参数、长度、轴、哈希和计时。只允许已验证复合查询的型号固件/连接；未知组合记录受阻。

`--compare-queries` 与 `--control` 互斥。既有 VXI-11/Socket 控制性能比较明确使用相同的逐条查询序列，避免把传输协议和查询策略的变化混成一种收益。普通只读性能入口按当前生产策略采集并核对设置未变。

当生产 VXI-11 已采用复合查询时，仅逐条协议对照不能证明 Socket 比现有生产路径更快，因此不签发优先连接资格。未来启用偏好还须另做实际生产策略的完整对照；已有多行选件边界限制不变。

从原生正式运行环境执行，配置文件仅包含一个 `rigol.dsa815`、`transport="visa"`、LAN `resource` 和完整 `expected_serial`。入口检查正式运行目录、库存与全局硬件锁的最终路径，不绕过 GUI/其他任务占用；打包应用的重定向目录不接受为正式实机目录。

```powershell
.venv-dev/Scripts/python.exe -m lab_control.instruments.devices.rigol_dsa815.hardware.validate_control --config <运行目录内的单设备配置.toml>
.venv-dev/Scripts/python.exe -m lab_control.instruments.devices.rigol_dsa815.hardware.validate_control --config <配置.toml> --control
.venv-dev/Scripts/python.exe -m lab_control.instruments.devices.rigol_dsa815.hardware.validate_control --config <配置.toml> --restore-from <历史批次目录>
```

默认只读预检，读取已有轨迹但不保存其样本。`--control` 需已确认 RF INPUT 无外部信号或接 50Ω 终端，允许改变普通扫描参数及刷新曲线。历史恢复先核对身份、固件和不可变备份哈希。复位、存储器保存/调用、轨迹写回及非本型号命令不在传输白名单内。

控制矩阵包含 100/110 MHz、1/2 MHz 跨度、99–101 MHz 端点，RBW 10/30 kHz、VBW 3/10 kHz 及自动状态，−20/−10 dBm 与 20/30 dB 衰减，单次/连续扫描，普通/采样检测、标记及峰值，轨迹模式，普通/零跨度各 10 帧，零跨度 0.1/0.2 s 与自动状态，纯监测 60 秒，恢复后三次重连。

先建立所有设置和四个标记的可靠备份、验证恢复目标。遇到选件模式、显示偏移、动态峰值跟踪、不可恢复标记或无法确认的运行状态时停止相关参数验证。超时、无效读回或不一致立即停止，无命令自动重发。恢复前读取实际状态，通信失败时最多一次身份核验后的恢复连接；恢复出错立即停止后续写入。

每批次保存 `results.json`、`summary.json`、`initial_snapshot.json`、`restoration-differences.json`、`communication.jsonl` 和 `record.md`，更新运行目录 `hardware-tests/index.md`。轨迹回复仅记录字节数及哈希；解析结果只记录点数、单位、轴和配置。没有自动曲线备份、导出或写回。

只有所有控制、采集、恢复、重连通过才能给出常用控制与轨迹采集通过结论。恢复设置不表示恢复旧曲线，噪声峰值不用于精度判定，物理精度始终另行验收。

## 通信性能验收

```powershell
.venv-dev/Scripts/python.exe -m lab_control.instruments.devices.rigol_dsa815.hardware.benchmark --config <运行目录内单设备配置.toml>
.venv-dev/Scripts/python.exe -m lab_control.instruments.devices.rigol_dsa815.hardware.benchmark --config <运行目录内单设备配置.toml> --control
```

默认入口只比较当前配置的完整/轻量读取，各 20 帧，不写设置。`--control` 执行 VXI-11 与 VISA SOCKET 的 ASC/REAL32、普通/零跨度、Auto 后 60 秒监测、恢复及每种传输三次重连；格式及显示刻度等补充状态也备份和恢复。附加命令依据编程手册 2-48、2-49、2-64、2-139，仅在验收 profile 开放，不扩大普通控制页面能力。

每批保存查询次数、耗时分布、实际完整/轻量帧数、设置时间、轴/单位、长度和哈希。完整采集与快速显示分别评价；自动周期完整帧计入实际监测耗时。仪器轨迹样本不落盘，文件保存路径在隔离 Mock 中验收。

只有所有分组通过且 ASC 完整采集中位耗时改善至少 20%、P95 不劣于基线时，才写入该资产的传输资格记录。记录含本机、VISA 库哈希、固件、适配器采集版本和证据目录，不绑定 IP。缺项或失败不生成通过资格。恢复失败停止后续写入，不自动再次恢复。

候选连接先独立读取恢复所需的全部设置。当前 Socket 多行选件表无法可靠分帧，在设置写入前阻止相关测试；速度达到门槛不代表获得资格。性能批次的 `initial_snapshot.json` 包含 `settings` 和 `extra`，不能直接交给旧控制入口的历史恢复模式。阶段变更时清除当前恢复成功标记，已通过的恢复快照归入该阶段历史，防止上一阶段的空差异覆盖本阶段失败。

### 二进制默认连接与缓存路径复核

普通连接已按用户要求默认启用 REAL32；本目录的只读、备份、格式比较及历史恢复入口均显式禁用连接初始化，保证先备份再写入。独立验证初始化时，先用只读连接备份，再重连调用正常初始化；已有 REAL32 应零格式写入，失败不重发。核对 ASC/REAL32 下完整帧和只读曲线各至少 20 帧，再验证连续记录、纯监测零周期参数查询及恢复。缓存帧单独计数，不沿用旧 23 次查询或 30 秒刷新结论。

使用生产连续采集入口进行实机性能检查时，必须在首次读取前替换 export 和 create_recording，安装只保留元数据的记录器；只替换旧 export 已不能覆盖分段记录。该入口默认会保存文件且返回分段路径。不得自动保存真实完整曲线，核验运行目录未生成额外数据文件。停止监测前关闭后台同步，避免将任务结束后合法恢复的低频读取误记为采集中查询。真实字节数、帧耗时、设置前后及清理证据仅在运行目录，截图与完整文件流程使用隔离 Mock。
