# 连接配置模板

复制所需 TOML 到 `%LOCALAPPDATA%\TF-LabControl\config`（或自己的 `LAB_CONTROL_HOME\config`），再填写真实资源、型号 ID 和序列号。模板地址使用文档保留网段，不代表已存在的设备。

| 模板 | 路线 | 修改项 |
| --- | --- | --- |
| [visa.toml](visa.toml) | VISA USB / LAN / GPIB | 完整资源、准确 adapter、expected_serial；USB/GPIB 使用扫描返回的地址 |
| [serial.toml](serial.toml) | PySerial / USB 虚拟串口 | COM、baud_rate、terminator；不会探测波特率 |
| [tcp.toml](tcp.toml) | 厂家明确支持的普通 TCP Socket | 主机、端口、adapter；不要用于 VICP |
| [vicp.toml](vicp.toml) | LeCroy VICP | 主机、1861 端口、准确 WaveRunner 型号 |
| [sdk.toml](sdk.toml) | PM100D / Pico | SDK 绝对路径、transport、资源和序列号 |
| [tserve.toml](tserve.toml) | 53100A 候选 TSERVE | 命令口、数据口、数据格式/周期、identity_confirmed |
| [mock.toml](mock.toml) | 完全隔离模拟 | 可直接用于 `--mock` CLI |

每个 `[[devices]]` 是一个连接配置。参数不允许包含 SCPI 命令；`adapter` 只能选注册 ID。`options` 仅在现场确认选件后填写，不会自动购买、安装或启用选件。`limits` 只能收紧型号已有数值边界，不能扩大；限制键使用能力名（例如 voltage/current）。不能用 IP/COM 号作为物理身份。

同一设备的其他资源可填写 `aliases = ["另一个已核实资源"]`。别名参与原子租约，防止扫描打开第二个句柄。未核实的别名不要填写。`expected_serial` 是连接校验条件，不能替代实机身份。

所有文件使用 UTF-8。Windows DLL 路径使用 TOML 单引号字符串，避免反斜杠转义。不要把现场资源和 SDK 路径提交进模板。
